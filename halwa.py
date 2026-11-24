import asyncio
import datetime
import discord
from discord.ext import commands,tasks
from discord import app_commands
from discord.ext.commands import MissingPermissions
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import logging
import time
import os
import threading
import csv
import io
import random
import re
from string import ascii_lowercase
from constants import constants
import json
import psutil
import state_snapshot
import sys

# Mutable state (lobby_*_teams.json, lobby_details*.json, *.csv, snapshot) is
# opened by bare relative filename throughout this file. In a container the app
# code sits in a read-only-ish image layer, so keep state on a mounted volume
# and run from there. STATE_DIR is left unset for a plain checkout, which keeps
# the original behaviour of running in the app directory.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = os.environ.get("STATE_DIR")
if _STATE_DIR:
    os.makedirs(_STATE_DIR, exist_ok=True)
    os.chdir(_STATE_DIR)
    print(f"State dir: {os.getcwd()}")
print(f"App dir: {_APP_DIR}")

class TeeLog:
    """Mirror a stream (stdout or stderr) into bot_logs.txt.
    Every write reaches the file immediately (the old logger buffered 50
    lines, so the lines right before a crash were lost), errors and
    tracebacks are included, and the file rolls over at 10 MB."""
    MAX_BYTES = 10 * 1024 * 1024
    _lock = threading.Lock()
    _fh = None

    def __init__(self, stream, filename):
        self.terminal = stream
        self.filename = filename

    @classmethod
    def _file(cls, filename):
        if cls._fh is None or cls._fh.closed:
            cls._fh = open(filename, "a", encoding="utf-8", buffering=1)
        return cls._fh

    def write(self, message):
        try:
            self.terminal.write(message)
        except Exception:
            pass
        if not message:
            return
        with TeeLog._lock:
            try:
                fh = TeeLog._file(self.filename)
                fh.write(message)
                if fh.tell() > TeeLog.MAX_BYTES:
                    fh.close()
                    os.replace(self.filename, self.filename + ".1")
                    TeeLog._fh = None
            except Exception:
                pass

    def flush(self):
        try:
            self.terminal.flush()
        except Exception:
            pass
        with TeeLog._lock:
            try:
                if TeeLog._fh and not TeeLog._fh.closed:
                    TeeLog._fh.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self.terminal, name)

sys.stdout = TeeLog(sys.stdout, "bot_logs.txt")
sys.stderr = TeeLog(sys.stderr, "bot_logs.txt")

# Configure logging
logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# Create a new instance of the Discord bot with command functionality
bot = commands.Bot(command_prefix='-', intents=intents)

# ---------------------------------------------------------------------------
# Shared UX / safety helpers
# ---------------------------------------------------------------------------

_bg_tasks = set()

def _spawn(coro):
    """Run a coroutine in the background and keep a reference so it is not GC'd."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task

def _atomic_write_json(path, data, indent=1):
    """Write JSON through a tmp file + os.replace. A crash mid-write can no
    longer leave a half-written lobby/claim file that fails to load."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)

def _has_bot_access(member):
    try:
        return any(r.name in constants.roles_for_bot_access for r in getattr(member, "roles", []) or [])
    except Exception:
        return False

async def _safe_reply(interaction, content=None, ephemeral=True, **kwargs):
    """Answer an interaction whether or not it was already responded to/deferred."""
    try:
        if interaction.response.is_done():
            kwargs.pop("delete_after", None)
            return await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
        return await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)
    except Exception as e:
        print(f"[reply] could not answer interaction: {e}")

async def _safe_delete(obj):
    try:
        if obj is not None:
            await obj.delete()
    except Exception:
        pass

def _schedule_thread_delete(thread, delay=0):
    """Delete a flow thread later without holding the user's process lock."""
    if thread is None:
        return None
    async def _later():
        try:
            if delay and delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await _safe_delete(thread)
    try:
        return _spawn(_later())
    except Exception as e:
        print(f"[thread-cleanup] schedule failed: {e}")

# --- per-user enroll/update/delete flows -----------------------------------
# constants.running_processes[user_id] = {"token", "kind", "thread_id", "started"}

class FlowCancelled(Exception):
    """Raised when a user stops their own enroll/update/delete flow."""

_flow_cancel_events = {}

async def _claim_process(user_id, kind):
    token = object()
    async with constants.running_processes_lock:
        constants.running_processes[user_id] = {
            "token": token, "kind": kind, "thread_id": None, "started": time.time(),
        }
    _flow_cancel_events[user_id] = asyncio.Event()
    return token

def _set_process_thread(user_id, token, thread_id):
    entry = constants.running_processes.get(user_id)
    if isinstance(entry, dict) and entry.get("token") is token:
        entry["thread_id"] = thread_id

async def _release_process(user_id, token):
    async with constants.running_processes_lock:
        entry = constants.running_processes.get(user_id)
        if isinstance(entry, dict) and entry.get("token") is token:
            constants.running_processes.pop(user_id, None)
            _flow_cancel_events.pop(user_id, None)

async def _stop_user_flow(user_id):
    """Stop whatever enroll/update/delete flow a user has open. Returns the
    entry that was stopped, or None. Used by the player's Stop button."""
    async with constants.running_processes_lock:
        entry = constants.running_processes.pop(user_id, None)
    ev = _flow_cancel_events.pop(user_id, None)
    if ev is not None:
        ev.set()
    if isinstance(entry, dict) and entry.get("thread_id"):
        thread = bot.get_channel(entry["thread_id"])
        if thread is None:
            try:
                thread = await bot.fetch_channel(entry["thread_id"])
            except Exception:
                thread = None
        _schedule_thread_delete(thread, 3)
    return entry if entry else None

def _thread_url(thread_id):
    return f"https://discord.com/channels/{constants.GUILD_ID}/{thread_id}"

class StopFlowView(discord.ui.View):
    """Shown when a user picks an option while a flow is still open."""
    def __init__(self, user_id, thread_id=None):
        super().__init__(timeout=180)
        self.user_id = user_id
        if thread_id:
            self.add_item(discord.ui.Button(label="Open my thread", style=discord.ButtonStyle.link, url=_thread_url(thread_id)))

    @discord.ui.button(label="Stop it and start over", style=discord.ButtonStyle.danger, emoji="\u23f9\ufe0f")
    async def stop_flow(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await _safe_reply(interaction, "This button isn't for you.", delete_after=10)
        stopped = await _stop_user_flow(self.user_id)
        msg = ("Stopped. Pick an option from the menu again to start fresh."
               if stopped else "Nothing was running anymore. Pick an option from the menu to start.")
        try:
            await interaction.response.edit_message(content=msg, view=None)
        except Exception:
            await _safe_reply(interaction, msg, delete_after=30)

class FlowCancelView(discord.ui.View):
    """Cancel button pinned at the top of every enroll/update/delete thread."""
    def __init__(self, user_id):
        super().__init__(timeout=1800)
        self.user_id = user_id

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="\u2716\ufe0f")
    async def cancel_flow(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await _safe_reply(interaction, "Only the person who started this can cancel it.", delete_after=10)
        ev = _flow_cancel_events.get(self.user_id)
        if ev is not None:
            ev.set()
        button.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            await _safe_reply(interaction, "Cancelling...", delete_after=5)

class YesNoView(discord.ui.View):
    """One-tap yes / no for questions asked inside a flow thread."""
    def __init__(self, user_id, timeout=300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.choice = None
        self.done = asyncio.Event()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _safe_reply(interaction, "Only the person who started this can answer.", delete_after=10)
            return False
        return True

    def lock(self, choice=None):
        for item in self.children:
            item.disabled = True
            if choice and getattr(item, "custom_id", None) and item.custom_id.endswith(choice):
                item.style = discord.ButtonStyle.success if choice == "yes" else discord.ButtonStyle.danger

    async def _pick(self, interaction, choice):
        self.choice = choice
        self.lock(choice)
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass
        self.done.set()
        self.stop()

    @discord.ui.button(label="Yes", emoji="\u2705", style=discord.ButtonStyle.secondary, custom_id="yn_yes")
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "yes")

    @discord.ui.button(label="No", emoji="\u274c", style=discord.ButtonStyle.secondary, custom_id="yn_no")
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "no")

_enroll_menu_reset_task = None

def _reset_enroll_menu(message):
    """Reset the enrollment dropdown so the same option can be picked again.
    Bursts of clicks are coalesced into one edit: the shared message is rate
    limited and editing it on every click made the menu feel sluggish."""
    global _enroll_menu_reset_task
    if message is None:
        return
    try:
        if _enroll_menu_reset_task is not None and not _enroll_menu_reset_task.done():
            return
        async def _do():
            await asyncio.sleep(1.5)
            try:
                await message.edit(view=TournamentView())
            except Exception as e:
                print(f"[enroll-menu] reset failed (non-fatal): {e}")
        _enroll_menu_reset_task = _spawn(_do())
    except Exception as e:
        print(f"[enroll-menu] schedule failed (non-fatal): {e}")

# Define the TournamentDropdown class inheriting from discord.ui.Select
class TournamentDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Enroll", value="enroll", description="Enroll your Team, GL Mate!", emoji="📑"),
            discord.SelectOption(label="Update", value="update", description="Update current Team", emoji="📝"),
            discord.SelectOption(label="Delete", value="delete", description="Delete current Team", emoji="🗑️")
        ]
        super().__init__(placeholder="Click here to select an action!", options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_value = self.values[0]
        user = interaction.user
        menu_message = interaction.message

        # A flow is already open: say where it is and offer a one-tap way out,
        # instead of a dead-end "wait up to 2 minutes".
        existing = constants.running_processes.get(user.id)
        if existing:
            kind = existing.get("kind", "process") if isinstance(existing, dict) else "process"
            thread_id = existing.get("thread_id") if isinstance(existing, dict) else None
            where = f" in <#{thread_id}>" if thread_id else ""
            await interaction.response.send_message(
                f"You already have a {kind} open{where}. Finish it there, or stop it and start over.",
                view=StopFlowView(user.id, thread_id), ephemeral=True, delete_after=180)
            _reset_enroll_menu(menu_message)
            return

        if str(user.id) in constants.blk_users_list:
            await interaction.response.send_message(f"Hey {user.mention} you are blacklisted as of now.", ephemeral=True, delete_after=30)
            _reset_enroll_menu(menu_message)
            return

        # Check if the user is already enrolled
        result = await isAlreadyEnrolled(user.id, returnTeamName=True, ctx_is_in_team=True)
        help_line = f"\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>."

        if selected_value == "enroll":
            if result:
                existing_team_message = f"{user.mention} Your enrollment can't proceed as either You or One of your teammates is already a part of some other team:\n"
                existing_team_message += result[0]
                existing_team_message += f"\nIf they're not a part of the listed team, reach out to the support team via <#{constants.HELP_CHANNEL_ID}>."
                await interaction.response.send_message(existing_team_message, ephemeral=True, delete_after=150)
                _reset_enroll_menu(menu_message)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            _reset_enroll_menu(menu_message)
            await enrollTeam(user, interaction)
            return

        if not result:
            await interaction.response.send_message(f"{user.mention} You aren't enrolled in any team as of now. Please select 'Enroll' to create one.", ephemeral=True, delete_after=15)
            _reset_enroll_menu(menu_message)
            return

        action = "update" if selected_value == "update" else "delete"
        if result[1] in constants.banned_team_list:
            await interaction.response.send_message(f"Sorry Mate {user.mention}, You can't {action} your team while it is banned.{help_line}", ephemeral=True, delete_after=40)
            _reset_enroll_menu(menu_message)
            return
        if result[1] in constants.cd_team_list:
            await interaction.response.send_message(f"Sorry Mate {user.mention}, You can't {action} your team while it is under cooldown.{help_line}", ephemeral=True, delete_after=40)
            _reset_enroll_menu(menu_message)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        _reset_enroll_menu(menu_message)
        if selected_value == "update":
            await updateTeam(user, result[0], interaction)
        else:
            await deleteTeam(user, result[0], interaction)

# Define the TournamentView class inheriting from discord.ui.View
class TournamentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Set timeout to None to make the view persistent
        self.add_item(TournamentDropdown())

def enrollment_embed():
    return discord.Embed(
        title="Team Enrollment",
        description=(
            "Select desirable option to create or edit your team details->\n\n"
            " a. **\"Enroll\"** -> CREATE NEW TEAM\n\n"
            " b. **\"Update\"** -> UPDATE YOUR TEAM\n\n"
            " c. **\"Delete\"** -> DELETE YOUR TEAM\n\n"
            "Use of alt accounts/Fake mentions are strictly prohibited and as you'll Get caught will be titled a Good Enough BAN.\n\n"
            "*> Atleast 4 players from your team should have **\"Verified\"** role on discord.*\n"
            "Changed your mind midway? Tap Cancel or type `cancel` in your thread, "
            "or pick an option here again and tap Stop it and start over."
        ),
        color=0x229db7,
    )

async def send_selectmenu(channel):
    view = TournamentView()  # Initialize TournamentView with timeout=None
    message = await channel.send(embed=enrollment_embed(), view=view)
    return message


def _reg_header(is_t3, live):
    if live:
        close_at = _auto_close_at(is_t3)
        tail = f", closes {_when(close_at)}" if close_at else ""
        return f"\U0001f7e2 **Registration is open now**{tail}"
    nxt = _next_reg_open()
    return f"Next registration opens {_when(nxt)}" if nxt else "**OPENS AT 12 PM**"

def reg_base_description(is_t3=False, live=False):
    enroll = constants.ENROLLMENT_CHANNEL_ID
    per = "1 lobby per day" if is_t3 else "1 lobby per group, so a team can register in both groups"
    intro = "" if is_t3 else "*Hey Wanderer, can I lurk on you :>*\n\n"
    return f'''{intro}{_reg_header(is_t3, live)}

1. Enroll your team first in <#{enroll}> (takes about 2 minutes).
2. Book only if your team will play. You can cancel from your lobby's slot list until 30 minutes before Match 1.
3. Fastest teams get the slots.
4. Booking has a quick captcha. Try it anytime with the TRIAL REG button.
5. One team can play {per}.'''

def _fill_bar(filled, size, width=10):
    filled = max(0, min(int(filled), int(size)))
    on = round(width * filled / size) if size else 0
    return "\u2588" * on + "\u2591" * (width - on)

def reg_slot_lines(is_t3=False):
    """Live fill per lobby, grouped, as a monospace block with bars."""
    lobby_map = constants.GROUP_LOBBY_MAP2 if is_t3 else constants.GROUP_LOBBY_MAP
    labels = constants.GROUP_LABELS2 if is_t3 else constants.GROUP_LABELS
    teams = constants.special_lobby_teams if is_t3 else constants.lobby_teams
    size = int(constants.SPECIAL_LOBBY_SIZE if is_t3 else constants.LOBBY_SIZE)
    out = []
    for group, lobbies in lobby_map.items():
        out.append(f"Group {group} \u00b7 {labels.get(group, '')}")
        for n in lobbies:
            idx = int(n) - 1
            filled = len(teams[idx]) if 0 <= idx < len(teams) else 0
            tail = "FULL" if filled >= size else f"{size - filled} left"
            out.append(f"  L{n} {_fill_bar(filled, size)} {filled:>2}/{size}  {tail}")
    return "\n\n```\n" + "\n".join(out) + "\n```"

_reg_last_edit = {False: 0.0, True: 0.0}
_reg_pending = {False: None, True: None}

async def _edit_reg_message(is_t3, show_slots):
    try:
        if is_t3:
            channel_id = constants.SPECIAL_REGISTRATION_CHANNEL_ID
            message_id = constants.SPECIAL_REG_MESSAGE_ID
        else:
            channel_id = constants.REGISTRATION_CHANNEL_ID
            message_id = constants.REG_MESSAGE_ID
        channel = bot.get_channel(channel_id)
        if not channel or not message_id:
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return
        desc = reg_base_description(is_t3, live=show_slots)
        if show_slots:
            desc += reg_slot_lines(is_t3)
        embed = discord.Embed(title='BookMySlot', description=desc, color=0x229db7)
        await message.edit(embed=embed)
    except Exception as e:
        print('[reg-slots] edit failed (non-fatal):', e)

async def _delayed_reg_refresh(is_t3, delay):
    await asyncio.sleep(delay)
    try:
        show = (not constants.special_disabled_status) if is_t3 else (not constants.disabled_status)
    except Exception:
        show = True
    _reg_last_edit[is_t3] = time.monotonic()
    await _edit_reg_message(is_t3, show)

async def refresh_reg_message(is_t3=False, show_slots=True, force=False):
    try:
        if force:
            _reg_last_edit[is_t3] = time.monotonic()
            await _edit_reg_message(is_t3, show_slots)
            return
        now = time.monotonic()
        elapsed = now - _reg_last_edit.get(is_t3, 0.0)
        if elapsed >= 30:
            _reg_last_edit[is_t3] = now
            await _edit_reg_message(is_t3, show_slots)
            return
        pending = _reg_pending.get(is_t3)
        if pending is None or pending.done():
            _reg_pending[is_t3] = asyncio.create_task(_delayed_reg_refresh(is_t3, 30 - elapsed))
    except Exception as e:
        print('[reg-slots] refresh failed (non-fatal):', e)

async def send_remenu(channel):
    embed = discord.Embed(title='BookMySlot', description=reg_base_description(is_t3=False), color=0x229db7)
    # 2. One team can participate once in a week, cooldowns refresh every Tuesday.
    view = RegistrationView2()  
    message = await channel.send(embed=embed, view=view)
    return message


async def send_remenu2(channel):
    embed = discord.Embed(title='BookMySlot', description=reg_base_description(is_t3=True), color=0x229db7)
    # 2. One team can participate once in a week, cooldowns refresh every Tuesday.
    view = RegistrationView3()  
    message = await channel.send(embed=embed, view=view)
    return message


        


class PracticeRegistrationButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'TRIAL REG', style=discord.ButtonStyle.primary,emoji=constants.practice_emoteid)

    async def callback(self, interaction: discord.Interaction):
        team_name = await validate_registration(interaction.user)
        if team_name:
            await interaction.response.send_modal(PracticeRegistrationModal())
        else: await interaction.response.send_message(f"You're not on a team yet. You or your IGL can enroll one in <#{constants.ENROLLMENT_CHANNEL_ID}> (about 2 minutes).", ephemeral=True,delete_after=60)

class PracticeRegistrationModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Practice captcha (nothing is booked)")
        self.word = ''.join(random.choice(ascii_lowercase) for _ in range(random.randint(5, 7)))
        self.nums = [random.randint(10,99) for _ in range(4)]
        
        # Add the captcha input fields
        self.sentence_input = discord.ui.TextInput(label=f"Type this word: {self.word}", placeholder=self.word, required=True, max_length=20)
        self.sum1_input = discord.ui.TextInput(label=f"{self.nums[0]} + {self.nums[1]} = ?", placeholder="Numbers only", required=True, max_length=6)
        self.sum2_input = discord.ui.TextInput(label=f"{self.nums[2]} + {self.nums[3]} = ?", placeholder="Numbers only", required=True, max_length=6)
    
        self.add_item(self.sentence_input)
        self.add_item(self.sum1_input)
        self.add_item(self.sum2_input)

    async def on_submit(self, interaction: discord.Interaction):
        problem = _captcha_problem(self.sentence_input.value, self.sum1_input.value, self.sum2_input.value, [self.word, *self.nums])
        if problem:
            await interaction.response.send_message(f"{problem} Tap TRIAL REG to try another one.", ephemeral=True, delete_after=30)
        else:
            await interaction.response.send_message("\u2705 Passed! The real one at 12 PM looks exactly like this, so be quick.", ephemeral=True, delete_after=20)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        if isinstance(error, ValueError):
            await _safe_reply(interaction, "Summation answers can be numbers only.", delete_after=240)
        else:
            await _safe_reply(interaction, "Something went wrong on our side. Please try again.", delete_after=240)
            print(f"An error occurred during practice session for {interaction.user}: {error}")
        

# ---------------------------------------------------------------------------
# Booking: one implementation for Open (is_t3=False) and T3 (is_t3=True).
# These used to be two copy-pasted sets of classes, which is how fixes kept
# landing in one path and not the other.
# ---------------------------------------------------------------------------

def _reg_key(is_t3):
    return "t3" if is_t3 else "open"

def _reg_kind(is_t3):
    return "T3" if is_t3 else "Open"

def _group_map(is_t3):
    return constants.GROUP_LOBBY_MAP2 if is_t3 else constants.GROUP_LOBBY_MAP

def _group_label(group, is_t3):
    return (constants.GROUP_LABELS2 if is_t3 else constants.GROUP_LABELS).get(group, "")

def _reg_set(is_t3):
    return constants.special_registered_set if is_t3 else constants.registered_set

def _reg_teams(is_t3):
    return constants.special_registered_teams if is_t3 else constants.registered_teams

def _lobbies(is_t3):
    return constants.special_lobby_teams if is_t3 else constants.lobby_teams

def _max_groups(is_t3):
    return constants.MAX_T3_GROUP_REGISTRATIONS if is_t3 else constants.MAX_GROUP_REGISTRATIONS

def _slots_limit(is_t3):
    return int(constants.SPECIAL_SLOTS_LIMIT if is_t3 else constants.SLOTS_LIMIT)

def _lobby_size(is_t3):
    return int(constants.SPECIAL_LOBBY_SIZE if is_t3 else constants.LOBBY_SIZE)

def _free_slots(lobby_number, is_t3):
    return available_slots2(lobby_number) if is_t3 else available_slots(lobby_number)

def _idp_channel_name(lobby_number, is_t3):
    return f"t3-idp-{lobby_number}" if is_t3 else f"group-{lobby_number}-idp"

def _groups_booked(team_name, is_t3):
    return sum(1 for entry in _reg_set(is_t3) if entry[0] == team_name)

async def _booking_block_reason(user, team_name, is_t3):
    """Why this team can't book right now, or None if it can."""
    help_line = f"\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>."
    if not team_name:
        return f"You're not on a team yet. You or your IGL can enroll one in <#{constants.ENROLLMENT_CHANNEL_ID}> (about 2 minutes)."
    if team_name in constants.banned_team_list:
        return f"{user.mention} Someone from your team is banned{_until_text(constants.ban_until.get(team_name))}.{help_line}"
    if team_name in constants.cd_team_list:
        return f"{user.mention} Someone from your team is on cooldown{_until_text(constants.cd_until.get(team_name))}, please wait for the cooldown period to end.{help_line}"
    if team_name == 'left_server':
        return f"{user.mention} A teammate isn't in this server right now. They need to rejoin before you can book."
    if is_t3 and any(role.name.startswith("Amateur") for role in getattr(user, "roles", [])):
        return f"{user.mention} You can't register here as you have an Amateur role."
    return None

class GroupButton(discord.ui.Button):
    def __init__(self, group, is_t3=False):
        super().__init__(
            label=f"Grp {group} - {_group_label(group, is_t3)}",
            style=discord.ButtonStyle.green,
            disabled=constants.special_disabled_status if is_t3 else constants.disabled_status,
        )
        self.group = group
        self.is_t3 = is_t3

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        if not _reg_open(self.is_t3):
            return await interaction.response.send_message(
                f"{_reg_kind(self.is_t3)} registration is closed right now. Cancelled slots show up in this channel to claim.",
                ephemeral=True, delete_after=30)
        team_name = await validate_registration(user)
        reason = await _booking_block_reason(user, team_name, self.is_t3)
        if reason:
            return await interaction.response.send_message(reason, ephemeral=True, delete_after=60)
        if (team_name, self.group) in _reg_set(self.is_t3):
            return await interaction.response.send_message(
                f"Your team already has a slot in Group {self.group} today. Tap My status to see it.",
                ephemeral=True, delete_after=120)
        booked = _groups_booked(team_name, self.is_t3)
        if booked >= _max_groups(self.is_t3):
            return await interaction.response.send_message(
                f"Your team is already booked in {booked} group(s) today (max {_max_groups(self.is_t3)}). Tap My status to see it.",
                ephemeral=True, delete_after=120)
        if not any(_free_slots(lobby, self.is_t3) > 0 for lobby in _group_map(self.is_t3)[self.group]):
            return await interaction.response.send_message(
                f"Group {self.group} is full. Cancelled slots show up in this channel to claim.",
                ephemeral=True, delete_after=30)
        await interaction.response.send_modal(GroupCaptchaModal(self.group, team_name, self.is_t3))

class GroupCaptchaModal(discord.ui.Modal):
    def __init__(self, group, team_name, is_t3=False):
        super().__init__(title=f"Book Group {group}: quick captcha"[:45])
        self.group = group
        self.team_name = team_name
        self.is_t3 = is_t3

        self.sentence_input = discord.ui.TextInput(
            label=f"Type this word: {constants.captcha_question_variables[0]}"[:45],
            placeholder=constants.captcha_question_variables[0], required=True, max_length=20
        )
        self.sum1_input = discord.ui.TextInput(
            label=f"{constants.captcha_question_variables[1]} + {constants.captcha_question_variables[2]} = ?",
            placeholder="Numbers only", required=True, max_length=6
        )
        self.sum2_input = discord.ui.TextInput(
            label=f"{constants.captcha_question_variables[3]} + {constants.captcha_question_variables[4]} = ?",
            placeholder="Numbers only", required=True, max_length=6
        )
        self.add_item(self.sentence_input)
        self.add_item(self.sum1_input)
        self.add_item(self.sum2_input)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        user_id = user.id
        is_t3 = self.is_t3

        problem = _captcha_problem(self.sentence_input.value, self.sum1_input.value, self.sum2_input.value)
        if problem:
            return await interaction.response.send_message(
                f"{problem} Tap Group {self.group} again to retry, quickly, slots go fast.", ephemeral=True, delete_after=30
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        timestamp_ms = datetime.datetime.now(tz=constants.timezone).strftime("%b %d %H:%M:%S.%f")
        group_lobbies = _group_map(is_t3)[self.group]
        locks = constants.group_locks2 if is_t3 else constants.group_locks
        assigned_lobby = None
        slot_no = None
        outcome = None

        async with locks[self.group]:
            reg_set = _reg_set(is_t3)
            if not _reg_open(is_t3):
                # Closed (full, auto-close or /break) while the captcha was open.
                # Booking now would land after the lobby files were written.
                outcome = "closed"
            elif (self.team_name, self.group) in reg_set:
                outcome = "same_group"
            elif _groups_booked(self.team_name, is_t3) >= _max_groups(is_t3):
                outcome = "limit"
            else:
                for lobby in group_lobbies:
                    if int(_free_slots(lobby, is_t3)) > 0:
                        assigned_lobby = lobby
                        break
                if not assigned_lobby:
                    outcome = "full"
                else:
                    row = await isAlreadyEnrolled(user_id, used2returnrow=True)
                    if not row:
                        outcome = "changed"
                    else:
                        _reg_teams(is_t3)[self.team_name] = row
                        reg_set.add((self.team_name, self.group))
                        lobby_dict = _lobbies(is_t3)[assigned_lobby - 1]
                        lobby_dict[self.team_name] = user_id
                        slot_no = list(lobby_dict.keys()).index(self.team_name) + 3
                        # Persist before anything slow (role, slot list edit): a crash
                        # after this point can no longer lose a confirmed booking.
                        state_snapshot.save_state_snapshot()

        if outcome == "closed":
            return await interaction.followup.send(
                f"{_reg_kind(is_t3)} registration closed while you were solving the captcha, so this booking was not made. Cancelled slots show up in this channel to claim.",
                ephemeral=True)
        if outcome == "same_group":
            return await interaction.followup.send(f"Your team already has a slot in Group {self.group} today.", ephemeral=True)
        if outcome == "limit":
            return await interaction.followup.send(
                f"Your team is already booked in the max {_max_groups(is_t3)} group(s) for today.", ephemeral=True)
        if outcome == "changed":
            return await interaction.followup.send(
                "Your team was changed (deleted/updated) while you were solving the captcha, so this booking was not saved. Please book again.",
                ephemeral=True)
        if outcome == "full":
            try:
                constants.late_attempts[_reg_key(is_t3)] = constants.late_attempts.get(_reg_key(is_t3), 0) + 1
            except Exception:
                pass
            await save_timestamp_to_csv(user, timestamp_ms, group_lobbies[0], "LATE")
            return await interaction.followup.send(
                f"Sorry, Group {self.group} filled up just before you. Cancelled slots show up in this channel to claim.", ephemeral=True)

        task1 = asyncio.create_task(
            interaction.followup.send(
                f'\u2705 Booked! "{self.team_name}" is in Group {self.group} ({_group_label(self.group, is_t3)}), '
                f'Lobby {assigned_lobby}, slot {slot_no:02}.\n'
                f'Your slot list and room IDs will be in {_channel_ref(assigned_lobby, is_t3)}. '
                f'Match 1 starts {_when(get_lobby_m1_start(assigned_lobby, is_t3))}.',
                ephemeral=True
            )
        )
        task3 = asyncio.create_task(assign_team_to_lobby(user, assigned_lobby, is_t3))
        task4 = asyncio.create_task(save_timestamp_to_csv(user, timestamp_ms, assigned_lobby, "BOOKED"))
        await asyncio.gather(task1, task3, task4)
        try:
            await refresh_live_slot_list(assigned_lobby, is_t3=is_t3)
        except Exception as e:
            print(f"[live-slots] refresh failed (non-fatal): {e}")

        if len(_reg_set(is_t3)) >= _slots_limit(is_t3):
            print(f"{_reg_kind(is_t3).upper()} REGISTRATION FULL")
            await finalize_registration(is_t3, reason="all lobbies full")

        print("Registration confirmed for user:", user_id)
        print(f"Assigned Lobby {assigned_lobby} in Group {self.group}")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        if isinstance(error, ValueError):
            await _safe_reply(interaction, "Summation answers can be numbers only. Tap the group button again to retry.", delete_after=240)
        else:
            await _safe_reply(interaction, "Something went wrong on our side while booking. Tap My status to check, then try again if you are not listed.", delete_after=240)
            print(f"An error occurred during registration for {interaction.user}: {error}")

class MyStatusButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="My status", style=discord.ButtonStyle.secondary, emoji="\U0001f9fe")

    async def callback(self, interaction: discord.Interaction):
        try:
            embed = await build_my_status_embed(interaction.user)
            await interaction.response.send_message(embed=embed, ephemeral=True, delete_after=180)
        except Exception as e:
            print(f"[my-status] {e}")
            await _safe_reply(interaction, "Couldn't load your status right now. Please try again in a moment.", delete_after=30)

class RegistrationView2(discord.ui.View):
    """Open registration buttons."""
    def __init__(self):
        super().__init__(timeout=None)
        for group in constants.GROUP_LOBBY_MAP.keys():
            self.add_item(GroupButton(group, is_t3=False))
        self.add_item(PracticeRegistrationButton())
        self.add_item(MyStatusButton())

class RegistrationView3(discord.ui.View):
    """T3 registration buttons."""
    def __init__(self):
        super().__init__(timeout=None)
        for group in constants.GROUP_LOBBY_MAP2.keys():
            self.add_item(GroupButton(group, is_t3=True))
        self.add_item(PracticeRegistrationButton())
        self.add_item(MyStatusButton())

def _view_for(is_t3):
    return RegistrationView3() if is_t3 else RegistrationView2()

class HowToPlayButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'How To Play', style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        embed = discord.Embed(title="How To Play", description=f"1. Get verified on [Trident Gaming](<https://tridentgaming.in/>) and claim your \"Verified\" role in discord from <#{constants.TICKET_CHANNEL_ID}>. Still confused how to do that?! There's a vid link beneath. Verification is a manual process and may take around 1-2 weeks.\n\n2. Enroll your team from <#{constants.ENROLLMENT_CHANNEL_ID}>, just have to select \"Enroll my team\" option from there, fill simple details, mention your teammates, and you're fine to Go, you can even Update/Delete your team later on.\n\n3. Book your slot for your preferred lobby from <#{constants.REGISTRATION_CHANNEL_ID}> at 12 PM Tuesday-Saturday. The buttons there will remain disabled whole time, and will open up at registration time.\n\n- [Click Me](<https://bit.ly/trident-verify-vid>) for tutorial on verification!\n- [Click Me](<https://bit.ly/trident-regi-vid>) for tutorial on registration!\n\n*Checkout <#1259394375880802434> channel.*", color=0x229db7)
        await interaction.response.send_message(embed=embed,ephemeral=True,delete_after=120)
    
class RulesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Scrims Rules', style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Scrims Rules", description=f"1. IGNs(In Game Name) of all players must have some similar pattern of characters as prefix/suffix, you'll be kicked from the room if not found such.\n\n2. All mic toxicity and rants are not allowed while in lobby or in match, you can be banned for this.\n\n3.  EMERGENCY PICKUP is prohibited, exploiting Bugs/Glitches or Hacking will lead to serious consequences.\n\n4. Complete POV recording is must for all the players of every team, this includes opening the game from play/app store and showing IMEI at end.\n\nMore rules are defined in <#1188850147958988851>.", color=0x229db7)
        await interaction.response.send_message(embed=embed,ephemeral=True,delete_after=180)

class PointsSystemButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Points System', style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"https://cdn.discordapp.com/attachments/1247528043455578152/1252305868767100949/Frame_16_1.png?ex=6671bc39&is=66706ab9&hm=96b83631ad2362319cd1a47bca5e00fb22eb49d6ba8db7db92024a0a46316233&",ephemeral=True,delete_after=180)

class ScheduleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Tier-3 Schedule', style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"https://cdn.discordapp.com/attachments/1264587995479146526/1277850094561136681/shs.jpg?ex=66ceaa23&is=66cd58a3&hm=f0b02f2e5ae14ca27ee9b7fc130caf6d05a967183477dc754b1c79336c2fe0fe&",ephemeral=True,delete_after=180)

class ScrimsOverviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(HowToPlayButton())
        self.add_item(RulesButton())
        self.add_item(PointsSystemButton())
        self.add_item(ScheduleButton())

class ExampleSsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Example Verification Screenshot', style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("https://cdn.discordapp.com/attachments/1187407524983480461/1220736410777157692/Screenshot_20240322_193829_Discord.jpg?ex=668c9c20&is=668b4aa0&hm=aedb3ec4440aad0c7c6578e3526f31368e6fc85f651d13989873c6f5e7a252a5&",ephemeral=True,delete_after=120)

class CheckVerificationButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Check Verification Status', style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        if any(role.name == constants.REQUIRED_ROLE_NAME for role in interaction.user.roles):
            await interaction.response.send_message("You have been verified and claimed your discord role as well.",ephemeral=True,delete_after=15)
        else:
            await interaction.response.send_message("You are not verified or havent claimed your role on discord.\n If you're \"Verified\" on website and still this came, follow the Step 2 listed above.",ephemeral=True,delete_after=45)

class bitInfoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'How can i Go to T1? Schedule?', row = 1,style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Checkout <#{constants.INFO_CHANNEL_ID}> channel.\nComplete details have been listed there.",ephemeral=True,delete_after=30)


class bitsMoreInfoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f"Time Taken for Verification", row = 1,style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Website Verification is a manual process and may take upto 7-14 days, you need to wait until its done.",ephemeral=True,delete_after=30)


class FaqView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ExampleSsButton())
        self.add_item(CheckVerificationButton())
        self.add_item(bitInfoButton())
        self.add_item(bitsMoreInfoButton())

def _lobby_role_name(lobby, is_t3):
    return f"T3 G{lobby} IDP" if is_t3 else f"Group {lobby} IDP"

def _is_idp_role_name(name):
    return bool(re.fullmatch(r"(Group \d+ IDP|T3 G\d+ IDP)", name or ""))

def _team_member_ids(team_name):
    """Discord ids (as str) of everyone on this team, from the cached sheet."""
    for row in (constants.cached_data or []):
        if len(row) > 1 and row[1] == team_name:
            return {str(x).strip() for x in row[2::2] if x and str(x).strip()}
    return set()

class TransferIDPButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label='Transfer IDP role', style=discord.ButtonStyle.grey, emoji="\U0001f501")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        held = [r for r in user.roles if _is_idp_role_name(r.name)]
        here = _lobby_from_channel_name(getattr(interaction.channel, "name", "") or "")
        if here and len(held) > 1:
            held = [r for r in held if r.name == _lobby_role_name(*here)] or held
        if not held:
            return await interaction.followup.send(
                "You don't have a lobby role to pass on. Only the player holding this lobby's IDP role can transfer it.", ephemeral=True)
        if len(held) > 1:
            return await interaction.followup.send(
                "You hold roles for more than one lobby. Tap this button in the lobby channel whose role you want to pass on.", ephemeral=True)
        try:
            result = await isAlreadyEnrolled(user.id, used2returnrowwithmessage=True, ctx_is_in_team=True)
        except Exception as e:
            print(f"[transfer] {e}")
            result = None
        if not result:
            return await interaction.followup.send("Couldn't find your team. If you just deleted or updated it, ask a mod.", ephemeral=True)
        view = PlayerSelectView(row=result[1], role=held[0], giver_id=user.id)
        if not view.children[0].options:
            return await interaction.followup.send("No teammate on your roster to pass it to.", ephemeral=True)
        await interaction.followup.send(
            f"{result[0]}\nPick the teammate who should get {held[0].name}. They'll see room IDs in this lobby, and you'll lose access.",
            view=view, ephemeral=True)

class PlayerSelectDropdown(discord.ui.Select):
    def __init__(self, row, role, giver_id=None):
        options = []
        self.role = role
        for i, (dc, ign) in enumerate(zip(row[2::2], row[3::2]), 1):
            if ign and dc and str(dc).strip() and str(dc).strip() != str(giver_id):
                options.append(discord.SelectOption(label=f"{i}. {ign}"[:100], value=str(dc).strip()))
        super().__init__(placeholder="Pick a teammate", options=options or [discord.SelectOption(label="No teammates", value="none")], disabled=not options)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        await interaction.response.defer(ephemeral=True)
        if self.role not in user.roles:
            return await interaction.followup.send(f"You don't hold {self.role.name} anymore, so there's nothing to transfer.", ephemeral=True)
        mate = bot.get_guild(constants.GUILD_ID).get_member(int(self.values[0]))
        if mate is None:
            return await interaction.followup.send("That teammate isn't in the server right now. They need to rejoin first.", ephemeral=True)
        try:
            # Give first, then take: if Discord fails halfway, nobody ends up without the role.
            await mate.add_roles(self.role, reason=f"IDP transfer from {user}")
            await user.remove_roles(self.role, reason=f"IDP transfer to {mate}")
        except discord.errors.Forbidden:
            return await interaction.followup.send("I'm missing permission to change roles. Please ask a mod.", ephemeral=True)
        except Exception as e:
            print(f"[transfer] {e}")
            return await interaction.followup.send("Discord didn't accept the change. Please try again in a minute.", ephemeral=True)
        try:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(
                f"{user.mention} passed {self.role.name} to {mate.mention}.",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except Exception:
            pass
        for item in self.view.children:
            item.disabled = True
        await interaction.edit_original_response(view=self.view)
        await interaction.followup.send(f"\u2705 Done. {mate.mention} now has {self.role.name}.", ephemeral=True)

class ModToolsButton(discord.ui.Button):
    """In an IDP channel: opens this lobby's desk. Every reply (copy names,
    bookers, room ID, add or remove team) stays in this channel, only for the mod."""
    def __init__(self):
        super().__init__(label='Mod Tools', style=discord.ButtonStyle.grey)

    async def callback(self, interaction: discord.Interaction):
        if not _is_staff(interaction.user):
            return await interaction.response.send_message("Mod Tools are for staff only.", ephemeral=True, delete_after=20)
        info = _lobby_from_channel_name(getattr(interaction.channel, "name", ""))
        if not info:
            return await _safe_reply(interaction, "Open this from a lobby IDP channel.", delete_after=20)
        await open_lobby_desk(interaction, info[0], info[1])

class AddTeamModal(discord.ui.Modal):
    def __init__(self, lobby_number, is_t3, member, prefill=""):
        label = f"T3 Lobby {lobby_number}" if is_t3 else f"Lobby {lobby_number}"
        super().__init__(title=f"Add team to {label}"[:45])
        self.lobby_number = lobby_number
        self.is_t3 = is_t3
        self.member = member
        self.team_input = discord.ui.TextInput(label=f"Team name (booker: {member.display_name})"[:45],
                                               default=(prefill or None), max_length=40, required=True)
        self.add_item(self.team_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = _idp_channel(self.lobby_number, self.is_t3)
        if channel is None:
            return await interaction.followup.send("Couldn't find that lobby's IDP channel.", ephemeral=True)
        try:
            await add_team_slotlist(self.team_input.value, self.member, channel, use_alt_lobby=self.is_t3)
        except AddTeamError as e:
            return await interaction.followup.send(f"Not added: {e}", ephemeral=True)
        await interaction.followup.send(f"\u2705 Added {self.team_input.value.strip()} ({self.member.mention}) to {channel.mention}.", ephemeral=True)
        try:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
                f"{self.team_input.value.strip()} added in {channel.mention} by {interaction.user.name}",
                allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[add-team] modal error: {error}")
        await _safe_reply(interaction, f"Couldn't add the team: {error}")

async def _open_add_team_modal(interaction, lobby_number, is_t3, picked):
    member = picked
    if not isinstance(member, discord.Member):
        guild = bot.get_guild(constants.GUILD_ID)
        member = guild.get_member(picked.id) if guild else None
    if member is None:
        return await _safe_reply(interaction, "That user isn't in the server.", delete_after=20)
    if _reg_open(is_t3):
        kind = "T3" if is_t3 else "Open"
        return await _safe_reply(interaction, f"{kind} registration is still running. Add Team unlocks as soon as it closes.", delete_after=30)
    prefill = await validate_registration(member, check_cooldown=False, check_left_server=False) or ""
    await interaction.response.send_modal(AddTeamModal(lobby_number, is_t3, member, prefill))

class AddTeamUserSelect(discord.ui.UserSelect):
    def __init__(self, lobby_number=None, is_t3=False, row=None):
        super().__init__(placeholder="Pick the team's booker (any player)", min_values=1, max_values=1, row=row)
        self.lobby_number = lobby_number
        self.is_t3 = is_t3

    async def callback(self, interaction: discord.Interaction):
        lobby = getattr(self.view, "lobby_number", None) or self.lobby_number
        is_t3 = getattr(self.view, "is_t3", self.is_t3)
        if not lobby:
            return await _safe_reply(interaction, "Pick the lobby first.", delete_after=15)
        await _open_add_team_modal(interaction, lobby, is_t3, self.values[0])

class IdpChannelTasksView(discord.ui.View):
    def __init__(self, cancel_disabled=False):
        super().__init__(timeout=None)
        self.add_item(TransferIDPButton())
        self.add_item(ModToolsButton())
        self.add_item(CancelSlotButton(disabled=cancel_disabled))

# ---------------------------------------------------------------------------
# Mod Panel: one persistent message (post it with /modpanel) so mods can do
# the common things in two taps instead of remembering commands.
# ---------------------------------------------------------------------------

def _count_real(teams):
    return sum(1 for v in (teams or {}).values() if v != "cancelled")

def _auto_close_at(is_t3):
    sched = constants.match_schedule_t3 if is_t3 else constants.match_schedule
    starts = [s for s in (get_lobby_m1_start(n, is_t3) for n in sched) if s is not None]
    if not starts:
        return None
    return min(starts) - datetime.timedelta(minutes=30)

def _lobby_counts(is_t3):
    return [_count_real(get_lobby_teams(i, is_t3)) for i in range(1, _lobby_count(is_t3) + 1)]

def _reg_summary_line(is_t3):
    counts = _lobby_counts(is_t3)
    booked = f"{sum(counts)}/{_slots_limit(is_t3)} booked"
    if _reg_open(is_t3):
        close_at = _auto_close_at(is_t3)
        tail = f", auto-closes {_when(close_at)}" if close_at else ""
        return f"\U0001f7e2 Open, {booked}{tail}"
    return f"Closed, {booked}"

def _reg_bars(is_t3):
    size = _lobby_size(is_t3)
    lines = []
    for group, lobbies in _group_map(is_t3).items():
        lines.append(f"Group {group} {_group_label(group, is_t3)}")
        for n in lobbies:
            c = _count_real(get_lobby_teams(n, is_t3))
            lines.append(f"  L{n} {_fill_bar(c, size)} {c:>2}/{size}")
    return "```\n" + "\n".join(lines) + "\n```"

def _reg_status_block(is_t3):
    return _reg_summary_line(is_t3) + "\n" + _reg_bars(is_t3)

def build_mod_status_embed():
    e = discord.Embed(title="Live status", color=0x229db7,
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    for is_t3 in (False, True):
        e.add_field(name=f"{_reg_kind(is_t3)} registration", value=_reg_status_block(is_t3), inline=True)
    cap = constants.captcha_question_variables
    if len(cap) >= 5:
        e.add_field(name="Captcha", value=f"`{cap[0]}`   {cap[1]} + {cap[2]} = {cap[1] + cap[2]}   {cap[3]} + {cap[4]} = {cap[3] + cap[4]}", inline=False)
    flows = list(constants.running_processes.items())
    if flows:
        now = time.time()
        shown = []
        for uid, entry in flows[:10]:
            if isinstance(entry, dict):
                mins = int((now - entry.get("started", now)) // 60)
                where = f" in <#{entry['thread_id']}>" if entry.get("thread_id") else ""
                shown.append(f"<@{uid}>  {entry.get('kind', 'flow')}, {mins} min{where}")
            else:
                shown.append(f"<@{uid}>")
        more = f"\n+{len(flows) - 10} more" if len(flows) > 10 else ""
        e.add_field(name=f"Enrolling right now ({len(flows)})", value="\n".join(shown) + more, inline=False)
    rows = constants.cached_data
    teams = f"{max(len(rows) - 1, 0)}" if rows else "loading"
    e.add_field(name="Teams", value=teams, inline=True)
    e.add_field(name="Banned / cooldown", value=f"{len(constants.banned_team_list)} / {len(constants.cd_team_list)}", inline=True)
    e.add_field(name="Blacklisted", value=str(len(constants.blk_users_list)), inline=True)
    try:
        age = int(time.time() - os.path.getmtime(state_snapshot._snapshot_path()))
        e.set_footer(text=f"Crash backup saved {age // 60} min {age % 60} s ago")
    except Exception:
        e.set_footer(text="No crash backup on disk yet")
    return e

def _find_team_rows(query, limit=5):
    rows = constants.cached_data or []
    q = (query or "").strip()
    ids = re.findall(r"\d{15,20}", q)
    hits = []
    if ids:
        for r in rows:
            if len(r) > 1 and any(i in r for i in ids):
                hits.append(r)
    else:
        want = _norm_team_name(q)
        exact = [r for r in rows if len(r) > 1 and _norm_team_name(r[1]) == want]
        partial = [r for r in rows if len(r) > 1 and want and want in _norm_team_name(r[1]) and r not in exact]
        hits = exact + partial
    return hits[:limit]

def _team_bookings(team_name):
    out = []
    for is_t3 in (False, True):
        for n in range(1, _lobby_count(is_t3) + 1):
            teams = get_lobby_teams(n, is_t3)
            if team_name in teams:
                slot = list(teams.keys()).index(team_name) + 3
                out.append(f"{'T3' if is_t3 else 'Open'} Lobby {n}, slot {slot:02}")
    return out

def build_team_embed(row):
    team_name = row[1]
    ids = [x for x in row[2::2]]
    igns = [x for x in row[3::2]]
    roster = []
    for k, (dc, ign) in enumerate(zip(ids, igns), 1):
        if dc and str(dc).strip():
            flag = " (blacklisted)" if str(dc) in constants.blk_users_list else ""
            busy = " (in a flow)" if str(dc).isdigit() and int(dc) in constants.running_processes else ""
            roster.append(f"{k}.  {ign or '?'}  \u00b7  <@{dc}>{flag}{busy}")
    e = discord.Embed(title=team_name, color=0x229db7)
    e.add_field(name="Players", value="\n".join(roster) or "No players listed", inline=False)
    if row and str(row[0]).isdigit():
        e.add_field(name="Created by", value=f"<@{row[0]}>", inline=True)
    status = []
    if team_name in constants.banned_team_list:
        status.append(f"\U0001f6ab Banned{_until_text(constants.ban_until.get(team_name))}")
    if team_name in constants.cd_team_list:
        status.append(f"\u23f3 On cooldown{_until_text(constants.cd_until.get(team_name))}")
    e.add_field(name="Status", value=", ".join(status) or "\u2705 Clear", inline=True)
    bookings = _team_bookings(team_name)
    e.add_field(name="Booked today", value="\n".join(bookings) or "Not booked", inline=False)
    return e

class FindTeamModal(discord.ui.Modal, title="Find a team"):
    query = discord.ui.TextInput(label="Team name, @mention or user ID", max_length=100, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        hits = _find_team_rows(self.query.value)
        if not hits:
            return await _safe_reply(interaction, f"No team matches `{self.query.value}`.", delete_after=60)
        embeds = [build_team_embed(r) for r in hits]
        await _safe_reply(interaction, f"{len(hits)} match(es):" if len(hits) > 1 else None, embeds=embeds,
                          allowed_mentions=discord.AllowedMentions.none())

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[find-team] {error}")
        await _safe_reply(interaction, f"Search failed: {error}")

def _lobby_options():
    opts = [discord.SelectOption(label=f"Open Lobby {n}", value=f"open:{n}",
                                 description=f"Group {get_group_for_lobby(n, False) or '?'}")
            for n in range(1, _lobby_count(False) + 1)]
    opts += [discord.SelectOption(label=f"T3 Lobby {n}", value=f"t3:{n}",
                                  description=f"Group {get_group_for_lobby(n, True) or '?'}")
             for n in range(1, _lobby_count(True) + 1)]
    return opts[:25]

def _parse_lobby_value(value):
    kind, n = value.split(":")
    return int(n), kind == "t3"

# ---------------------------------------------------------------------------
# Lobby desk: everything a mod does for one lobby, in one ephemeral card.
# Opened from the Mod Panel's lobby picker or from Mod Tools in an IDP
# channel (then every reply lands in that same channel, only for that mod).
# ---------------------------------------------------------------------------

def _is_staff(member):
    try:
        return _has_bot_access(member) or bool(member.guild_permissions.manage_roles)
    except Exception:
        return _has_bot_access(member)

def _lobby_title(lobby, is_t3):
    group = get_group_for_lobby(lobby, is_t3) or "?"
    return f"{_reg_kind(is_t3)} Lobby {lobby}  \u00b7  Group {group}  \u00b7  {_group_label(group, is_t3)}"

def _lobby_matches(lobby, is_t3):
    return (constants.match_schedule_t3 if is_t3 else constants.match_schedule).get(int(lobby), {})

def _cancel_state(lobby, is_t3):
    if _reg_open(is_t3):
        return "Opens when registration closes"
    if is_lobby_started(lobby, is_t3):
        return "Closed, match started"
    if is_before_lobby_cancel_deadline(lobby, is_t3):
        return f"Open until {_when(get_lobby_cancel_deadline(lobby, is_t3))}"
    return "Closed"

def copy_names_text(lobby, is_t3):
    teams = get_lobby_teams(lobby, is_t3)
    if not teams:
        return f"{_reg_kind(is_t3)} Lobby {lobby} has no teams yet."
    names = "\n".join(teams.keys())
    return f"{_reg_kind(is_t3)} Lobby {lobby}, {_count_real(teams)} teams, slot order:\n```\n{names}\n```"[:1990]

def bookers_text(lobby, is_t3):
    teams = get_lobby_teams(lobby, is_t3)
    if not teams:
        return f"{_reg_kind(is_t3)} Lobby {lobby} has no teams yet."
    lines = []
    for slot, (name, uid) in enumerate(teams.items(), start=3):
        who = f"<@{uid}>" if str(uid).isdigit() else "free slot"
        lines.append(f"`{slot:02}`  {name}  \u00b7  {who}")
    return "\n".join(lines)[:1990]

def build_lobby_desk_embed(lobby, is_t3):
    teams = get_lobby_teams(lobby, is_t3)
    real = _count_real(teams)
    freed = len(teams) - real
    size = _lobby_size(is_t3)
    state = "Registration open, filling live" if _reg_open(is_t3) else "Registration closed"
    channel = _idp_channel(lobby, is_t3)
    e = discord.Embed(title=_lobby_title(lobby, is_t3), color=0x229db7)
    e.description = f"{state}\n{real}/{size} teams" + (f", {freed} freed slot(s)" if freed else "") + (f"  \u00b7  {channel.mention}" if channel else "")
    matches = _lobby_matches(lobby, is_t3)
    if matches:
        e.add_field(name="Matches", value=_match_lines(matches), inline=False)
    e.add_field(name="Cancel", value=_cancel_state(lobby, is_t3), inline=True)
    if teams:
        rows = [f"{slot:02}  {name}" for slot, name in enumerate(teams.keys(), start=3)]
        body = "\n".join(rows)
        if len(body) > 1000:
            body = body[:990] + "\n..."
        e.add_field(name="Slots", value=f"```\n{body}\n```", inline=False)
    else:
        e.add_field(name="Slots", value="No teams yet", inline=False)
    e.set_footer(text="Only you can see this. Buttons act on this lobby.")
    return e

class DeskButton(discord.ui.Button):
    def __init__(self, label, action, style=discord.ButtonStyle.secondary, row=0, emoji=None, payload=None, disabled=False):
        super().__init__(label=label, style=style, row=row, emoji=emoji, disabled=disabled)
        self.action = action
        self.payload = payload

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle(interaction, self.action, self.payload)

class DeskSwitchSelect(discord.ui.Select):
    def __init__(self, lobby, is_t3):
        opts = _lobby_options()
        current = f"{'t3' if is_t3 else 'open'}:{lobby}"
        for o in opts:
            o.default = (o.value == current)
        super().__init__(placeholder="Switch lobby", options=opts, row=3)

    async def callback(self, interaction: discord.Interaction):
        lobby, is_t3 = _parse_lobby_value(self.values[0])
        await interaction.response.edit_message(embed=build_lobby_desk_embed(lobby, is_t3), view=LobbyDeskView(lobby, is_t3))

class LobbyDeskView(discord.ui.View):
    def __init__(self, lobby, is_t3):
        super().__init__(timeout=900)
        self.lobby = int(lobby)
        self.is_t3 = is_t3
        self.lobby_number = self.lobby     # read by AddTeamUserSelect
        self.add_item(DeskButton("Copy names", "copy", discord.ButtonStyle.primary, 0, "\U0001f4cb"))
        self.add_item(DeskButton("Bookers", "bookers", row=0, emoji="\U0001f465"))
        self.add_item(DeskButton("Reload", "reload", row=0, emoji="\U0001f504"))
        self.add_item(DeskButton("Resync slot list", "resync", row=0))
        for m, info in list(_lobby_matches(self.lobby, is_t3).items())[:4]:
            self.add_item(DeskButton(f"Room ID M{m}  {info.get('st', '')}", "room", discord.ButtonStyle.success, 1, payload=m))
        closed = not _reg_open(is_t3)
        self.add_item(DeskButton("Add team", "add", row=2, emoji="\u2795", disabled=not closed))
        self.add_item(DeskButton("Remove team", "remove", discord.ButtonStyle.danger, 2, disabled=not closed))
        self.add_item(DeskButton("Message lobby", "message", row=2, emoji="\U0001f4e3"))
        if not is_t3:
            self.add_item(DeskButton("Promote winner", "promote", row=2, emoji="\U0001f3c6", disabled=not closed))
        self.add_item(DeskSwitchSelect(self.lobby, is_t3))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_staff(interaction.user):
            return True
        await _safe_reply(interaction, "Mods only.", delete_after=15)
        return False

    async def handle(self, interaction, action, payload):
        lobby, is_t3 = self.lobby, self.is_t3
        if action in ("add", "remove", "message", "promote", "room") and not _has_bot_access(interaction.user):
            return await _safe_reply(interaction, "That needs a scrim manager role.", delete_after=20)
        if action == "copy":
            return await _safe_reply(interaction, copy_names_text(lobby, is_t3))
        if action == "bookers":
            return await _safe_reply(interaction, bookers_text(lobby, is_t3), allowed_mentions=discord.AllowedMentions.none())
        if action == "reload":
            return await interaction.response.edit_message(embed=build_lobby_desk_embed(lobby, is_t3), view=LobbyDeskView(lobby, is_t3))
        if action == "resync":
            await interaction.response.defer(ephemeral=True, thinking=True)
            if _reg_open(is_t3):
                await refresh_live_slot_list(lobby, is_t3=is_t3)
            else:
                await update_slot_list_after_cancel(lobby, is_t3)
            return await interaction.followup.send("Slot list in the IDP channel now matches the lobby data.", ephemeral=True)
        if action == "room":
            info = _lobby_matches(lobby, is_t3).get(payload)
            if not info:
                return await _safe_reply(interaction, "That match isn't in the schedule.", delete_after=20)
            return await interaction.response.send_modal(RoomIdModal(is_t3, lobby, payload, info))
        if _reg_open(is_t3):
            return await _safe_reply(interaction, f"{_reg_kind(is_t3)} registration is still running. This unlocks as soon as it closes.", delete_after=30)
        if action == "add":
            return await _safe_reply(interaction, f"Adding a team to {_reg_kind(is_t3)} Lobby {lobby}. Pick the booker, then confirm the team name.",
                                     view=_single_select_view(AddTeamUserSelect(lobby, is_t3), lobby, is_t3), delete_after=300)
        if action == "remove":
            teams = [n for n, v in get_lobby_teams(lobby, is_t3).items() if v != "cancelled"]
            if not teams:
                return await _safe_reply(interaction, "No teams to remove.", delete_after=20)
            return await _safe_reply(interaction, "Which team should leave this lobby?",
                                     view=_single_select_view(RemoveTeamSelect(lobby, is_t3, teams), lobby, is_t3), delete_after=300)
        if action == "message":
            return await interaction.response.send_modal(LobbyMessageModal(lobby, is_t3))
        if action == "promote":
            teams = [n for n, v in get_lobby_teams(lobby, is_t3).items() if v != "cancelled"]
            if not teams:
                return await _safe_reply(interaction, "No teams in this lobby.", delete_after=20)
            return await _safe_reply(interaction, "Who won this lobby? They get the T3 Verified role and an announcement in the IDP channel.",
                                     view=_single_select_view(PromoteSelect(lobby, teams), lobby, is_t3), delete_after=300)

def _single_select_view(item, lobby, is_t3):
    v = discord.ui.View(timeout=300)
    v.lobby_number = lobby
    v.is_t3 = is_t3
    v.add_item(item)
    return v

def _team_options(teams):
    return [discord.SelectOption(label=n[:100], value=n[:100], description=f"Slot {slot:02}")
            for slot, n in enumerate(teams, start=3)][:25]

class RemoveTeamSelect(discord.ui.Select):
    def __init__(self, lobby, is_t3, teams):
        super().__init__(placeholder="Pick the team", options=_team_options(teams))
        self.lobby, self.is_t3 = lobby, is_t3

    async def callback(self, interaction: discord.Interaction):
        team = self.values[0]
        started = is_lobby_started(self.lobby, self.is_t3)
        note = ("The match has started, so no claim message will be posted." if started
                else "Remove and post claim frees the slot for another team.")
        await interaction.response.edit_message(
            content=f"Remove {team} from {_reg_kind(self.is_t3)} Lobby {self.lobby}? Their IDP role is taken away. {note}",
            view=RemoveConfirmView(team, self.lobby, self.is_t3, allow_claim=not started))

class RemoveConfirmView(discord.ui.View):
    def __init__(self, team, lobby, is_t3, allow_claim=True):
        super().__init__(timeout=120)
        self.team, self.lobby, self.is_t3 = team, lobby, is_t3
        if allow_claim:
            b = discord.ui.Button(label="Remove and post claim", style=discord.ButtonStyle.danger)
            b.callback = lambda i: self._go(i, True)
            self.add_item(b)
        b2 = discord.ui.Button(label="Remove, no claim", style=discord.ButtonStyle.secondary if allow_claim else discord.ButtonStyle.danger)
        b2.callback = lambda i: self._go(i, False)
        self.add_item(b2)
        b3 = discord.ui.Button(label="Keep them", style=discord.ButtonStyle.secondary)
        b3.callback = self._keep
        self.add_item(b3)

    async def _go(self, interaction, post_claim):
        if not _has_bot_access(interaction.user):
            return await _safe_reply(interaction, "That needs a scrim manager role.", delete_after=20)
        await interaction.response.edit_message(content=f"Removing {self.team}...", view=None)
        ok, msg = await cancel_team_slot(self.team, self.lobby, self.is_t3, post_claim=post_claim,
                                         by_label=f"a mod ({interaction.user.display_name})")
        await interaction.edit_original_response(content=msg)
        if ok:
            try:
                await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
                    f"{interaction.user.name} removed {self.team} from {_reg_kind(self.is_t3)} Lobby {self.lobby}"
                    + (" and posted a claim" if post_claim else ""), allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass

    async def _keep(self, interaction):
        await interaction.response.edit_message(content="Nothing changed.", view=None)

class LobbyMessageModal(discord.ui.Modal):
    def __init__(self, lobby, is_t3):
        super().__init__(title=f"Message {_reg_kind(is_t3)} Lobby {lobby}"[:45])
        self.lobby, self.is_t3 = lobby, is_t3
        self.text = discord.ui.TextInput(label="Message (the lobby role is pinged)", style=discord.TextStyle.paragraph,
                                         max_length=1500, required=True)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        channel = _idp_channel(self.lobby, self.is_t3)
        if channel is None:
            return await _safe_reply(interaction, "Couldn't find that lobby's IDP channel.")
        role_name = f"T3 G{self.lobby} IDP" if self.is_t3 else f"Group {self.lobby} IDP"
        role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=role_name)
        await channel.send(f"{role.mention if role else ''}\n{self.text.value.strip()}",
                           allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
        await _safe_reply(interaction, f"Sent in {channel.mention}.", delete_after=30)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[lobby-message] {error}")
        await _safe_reply(interaction, f"Couldn't send it: {error}")

class PromoteSelect(discord.ui.Select):
    def __init__(self, lobby, teams):
        super().__init__(placeholder="Pick the winning team", options=_team_options(teams))
        self.lobby = lobby

    async def callback(self, interaction: discord.Interaction):
        team = self.values[0]
        await interaction.response.edit_message(content=f"Promoting {team}...", view=None)
        try:
            await share_lobby_results(self.lobby, team)
            msg = f"\u2705 {team} promoted to Tier 3 and announced in group-{self.lobby}-idp."
        except Exception as e:
            print(f"[promote] {e}")
            msg = f"Couldn't promote {team}: {e}"
        await interaction.edit_original_response(content=msg)

async def open_lobby_desk(interaction, lobby, is_t3):
    await _safe_reply(interaction, embed=build_lobby_desk_embed(lobby, is_t3), view=LobbyDeskView(lobby, is_t3),
                      allowed_mentions=discord.AllowedMentions.none())

class PanelDeskSelect(discord.ui.Select):
    """Persistent lobby picker on the Mod Panel."""
    def __init__(self):
        super().__init__(placeholder="Open a lobby desk: slots, copy names, room IDs, add or remove teams",
                         options=_lobby_options(), custom_id="modpanel:desk", row=0)

    async def callback(self, interaction: discord.Interaction):
        lobby, is_t3 = _parse_lobby_value(self.values[0])
        await open_lobby_desk(interaction, lobby, is_t3)

class ConfirmCloseView(discord.ui.View):
    def __init__(self, is_t3):
        super().__init__(timeout=60)
        self.is_t3 = is_t3

    @discord.ui.button(label="Yes, close it now", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_bot_access(interaction.user):
            return await _safe_reply(interaction, "Mods only.", delete_after=15)
        kind = "T3" if self.is_t3 else "Open"
        if not _reg_open(self.is_t3):
            return await interaction.response.edit_message(content=f"{kind} registration is already closed.", view=None)
        await interaction.response.edit_message(content=f"Closing {kind} registration and posting slot lists...", view=None)
        try:
            closed = await finalize_registration(self.is_t3, reason=f"Mod Panel, {interaction.user.name}")
            if closed:
                await interaction.followup.send(f"\u2705 {kind} registration closed. Cancel buttons are open in the IDP channels and the summary is in <#{constants.UPDATES_CHANNEL_ID}>.", ephemeral=True)
            else:
                await interaction.followup.send(f"{kind} registration was already closed.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Close failed: {e}", ephemeral=True)

    @discord.ui.button(label="Keep it open", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="OK, left it open.", view=None)

def _my_next_step(problems, booked):
    if problems:
        return "Sort out the problem above first, or ask a mod if it looks wrong."
    if booked:
        is_t3, n, slot = booked[0]
        first = _lobby_matches(n, is_t3).get(1, {})
        idt = _ist_today(first.get("idt"))
        channel = _idp_channel(n, is_t3)
        where = channel.mention if channel else "your lobby's IDP channel"
        drop = f" at {_when(idt)}" if idt else ""
        return f"Match 1 room ID drops in {where}{drop}. Join 2 minutes early and sit in slot {slot:02}."
    for is_t3 in (False, True):
        if _reg_open(is_t3):
            reg = constants.SPECIAL_REGISTRATION_CHANNEL_ID if is_t3 else constants.REGISTRATION_CHANNEL_ID
            close_at = _auto_close_at(is_t3)
            tail = f", it closes {_when(close_at)}" if close_at else ""
            return f"{_reg_kind(is_t3)} registration is open. Book in <#{reg}>{tail}."
    nxt = _next_reg_open()
    return f"Next registration opens {_when(nxt)}." if nxt else "Registration is closed for now."

async def build_my_status_embed(user):
    """Everything a player usually asks a mod: team, today's slot, deadlines."""
    team = await validate_registration(user, check_cooldown=False, check_left_server=False)
    if not team:
        return discord.Embed(
            title="You're not in a team yet",
            description=f"Enroll your team from <#{constants.ENROLLMENT_CHANNEL_ID}> (pick Enroll in the menu). It takes about 2 minutes.",
            color=0xf0b232)
    e = discord.Embed(title=team, color=0x229db7)
    row = next((r for r in (constants.cached_data or []) if str(user.id) in r), None)
    if row:
        roster = [f"{ign or '?'}" for dc, ign in zip(row[2::2], row[3::2]) if dc and str(dc).strip()]
        e.description = "  \u00b7  ".join(roster)
    problems = []
    if team in constants.banned_team_list:
        problems.append(f"\U0001f6ab Banned{_until_text(constants.ban_until.get(team))}")
    if team in constants.cd_team_list:
        problems.append(f"\u23f3 On cooldown{_until_text(constants.cd_until.get(team))}")
    if await validate_registration(user) == 'left_server':
        problems.append("\u26a0\ufe0f A teammate left the server; they must rejoin before you can book")
    e.add_field(name="Can book", value="\n".join(problems) if problems else "\u2705 Yes", inline=False)

    booked = []
    for is_t3 in (False, True):
        for n in range(1, _lobby_count(is_t3) + 1):
            teams = get_lobby_teams(n, is_t3)
            if team in teams:
                booked.append((is_t3, n, list(teams.keys()).index(team) + 3))
    if not booked:
        e.add_field(name="Today", value="Not booked", inline=False)
    for is_t3, n, slot in booked:
        group = get_group_for_lobby(n, is_t3) or "?"
        start = get_lobby_m1_start(n, is_t3)
        channel = _idp_channel(n, is_t3)
        e.add_field(name="Slot", value=f"{_reg_kind(is_t3)} \u00b7 Group {group} \u00b7 Lobby {n} \u00b7 {slot:02}", inline=False)
        e.add_field(name="Match 1", value=_when(start) if start else "?", inline=True)
        e.add_field(name="Cancel", value=_cancel_state(n, is_t3), inline=True)
        e.add_field(name="Channel", value=channel.mention if channel else "?", inline=True)
    e.add_field(name="What to do now", value=_my_next_step(problems, booked), inline=False)
    state = []
    for is_t3 in (False, True):
        state.append(f"{_reg_kind(is_t3)} {'open' if _reg_open(is_t3) else 'closed'}")
    e.set_footer(text="Registration: " + ",  ".join(state))
    return e

class LiftKindSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="What do you want to lift?", options=[
            discord.SelectOption(label="Ban", value="ban", description="Team ban", emoji="\U0001f6ab"),
            discord.SelectOption(label="Cooldown", value="cooldown", description="Team cooldown", emoji="\u23f3"),
            discord.SelectOption(label="Blacklist", value="blacklist", description="User blacklist", emoji="\u26d4"),
        ])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LiftModal(self.values[0]))

class LiftView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(LiftKindSelect())

class LiftModal(discord.ui.Modal):
    def __init__(self, kind):
        super().__init__(title=f"Lift {kind}")
        self.kind = kind
        label = "@mention or user ID" if kind == "blacklist" else "Team name, @mention or user ID"
        self.target = discord.ui.TextInput(label=label, max_length=100, required=True)
        self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            removed, what = await lift_restriction(self.kind, self.target.value)
        except ValueError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        if not removed:
            return await interaction.followup.send(f"No active {self.kind} found for {what}.", ephemeral=True,
                                                   allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(f"\u2705 Lifted {self.kind} for {what} ({removed} row(s) removed from the sheet).",
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        try:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
                f"{interaction.user.name} lifted {self.kind} for {what}", allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[lift] {error}")
        await _safe_reply(interaction, f"Couldn't lift it: {error}")

# ---------------------------------------------------------------------------
# Room ID posting: replaces the old disabled per-lobby loops.
# ---------------------------------------------------------------------------

def _match_options():
    opts = []
    for is_t3 in (False, True):
        sched = constants.match_schedule_t3 if is_t3 else constants.match_schedule
        for lobby, matches in sched.items():
            for m, info in matches.items():
                opts.append((info.get("st", ""), is_t3, lobby, m, info))
    def _key(o):
        try:
            return datetime.datetime.strptime(o[0], "%I:%M %p").time()
        except Exception:
            return datetime.time(23, 59)
    opts.sort(key=_key)
    return opts

def _room_start_time(info):
    """Scheduled start, or now + 5 min if the ID is posted after the ID time."""
    now = datetime.datetime.now(tz=constants.timezone)
    try:
        st = datetime.datetime.strptime(info["st"], "%I:%M %p")
        idt = datetime.datetime.strptime(info["idt"], "%I:%M %p")
        if now.time() >= idt.time():
            return (now + datetime.timedelta(minutes=5)).strftime("%I:%M %p")
        return st.strftime("%I:%M %p")
    except Exception:
        return info.get("st", "")

_MAP_COLORS = {"ERANGEL": 0x3fa34d, "MIRAMAR": 0xd9a441, "RONDO": 0x3d8fd6, "SANHOK": 0x2e8b57, "VIKENDI": 0xbfd7ea, "LIVIK": 0x8e6bd6}

def _slot_list_url(lobby, is_t3):
    """Jump link to the lobby's slot list message, if we know where it is."""
    entry = None
    mem = constants.temp_json_dict2 if is_t3 else constants.temp_json_dict
    entry = (mem or {}).get(lobby) or (mem or {}).get(str(lobby))
    if not entry:
        try:
            with open("lobby_details2.json" if is_t3 else "lobby_details.json", "r") as f:
                entry = json.load(f).get(str(lobby))
        except Exception:
            entry = None
    try:
        return f"https://discord.com/channels/{constants.GUILD_ID}/{int(entry[1])}/{int(entry[0])}"
    except Exception:
        return None

def room_message(is_t3, lobby, match_no, info, room_id, password, start):
    """(content, embed) for a room ID post. Content carries the ping and a
    one-line preview; the embed puts ID and password in their own boxes."""
    role_name = f"T3 G{lobby} IDP" if is_t3 else f"Group {lobby} IDP"
    guild = bot.get_guild(constants.GUILD_ID)
    role = discord.utils.get(guild.roles, name=role_name) if guild else None
    header = "TRIDENT ESPORTS T3 SCRIMS" if is_t3 else "TRIDENT ESPORTS TIER 3 SCRIMS"
    game_map = str(info.get("map", "?")).upper()
    start_dt = _ist_today(start)
    content = f"{role.mention if role else role_name}  Match {int(match_no)} on {game_map.title()}: room is up, ID and password below."
    e = discord.Embed(title=f"MATCH {int(match_no):02}  \u00b7  {game_map}", color=_MAP_COLORS.get(game_map, 0x229db7))
    if start_dt:
        join_by = start_dt - datetime.timedelta(minutes=2)
        e.description = f"Starts {_when(start_dt)}\nBe in the room by {_ts(join_by)}"
    else:
        e.description = f"Starts {start}\nBe in the room 2 minutes before"
    e.add_field(name="Room ID", value=f"```{room_id}```", inline=True)
    e.add_field(name="Password", value=f"```{password}```", inline=True)
    url = _slot_list_url(lobby, is_t3)
    slot_line = f"Sit in your allotted slot ([slot list]({url}))" if url else "Sit in your allotted slot (see the slot list above)"
    e.add_field(name="Before you join", value=(
        f"1. {slot_line}\n"
        "2. Every player records POV. We may ask for raw POV anytime.\n"
        "3. No emergency pickups.\n"
        "4. Missing a match means a team ban."), inline=False)
    e.set_footer(text=f"{header}  \u00b7  {_reg_kind(is_t3)} Lobby {lobby}")
    return content, e

class RoomIdModal(discord.ui.Modal):
    def __init__(self, is_t3, lobby, match_no, info):
        super().__init__(title=f"{'T3 ' if is_t3 else ''}Lobby {lobby}, Match {match_no} ({info.get('map', '?')})"[:45])
        self.is_t3, self.lobby, self.match_no, self.info = is_t3, lobby, match_no, info
        self.room_id = discord.ui.TextInput(label="Room ID", max_length=20, required=True)
        self.password = discord.ui.TextInput(label="Password", default=f"TG{lobby}{lobby}{lobby}", max_length=30, required=True)
        self.start = discord.ui.TextInput(label="Start time", default=_room_start_time(info), max_length=12, required=True)
        for item in (self.room_id, self.password, self.start):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        room_id = self.room_id.value.strip()
        if not room_id.isdigit():
            return await _safe_reply(interaction, f"Room ID should be digits only, got `{room_id}`. Nothing was posted.", delete_after=60)
        channel = _idp_channel(self.lobby, self.is_t3)
        if channel is None:
            return await _safe_reply(interaction, "Couldn't find that lobby's IDP channel.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        content, embed = room_message(self.is_t3, self.lobby, self.match_no, self.info, room_id, self.password.value.strip(), self.start.value.strip())
        await channel.send(content, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
        await interaction.followup.send(f"\u2705 Room ID posted in {channel.mention} (start {self.start.value.strip()}).", ephemeral=True)
        try:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
                f"Room ID for {_reg_kind(self.is_t3)} Lobby {self.lobby} M{self.match_no} posted by {interaction.user.name}",
                allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[room-id] {error}")
        await _safe_reply(interaction, f"Couldn't post the room ID: {error}")

class RoomMatchSelect(discord.ui.Select):
    def __init__(self, is_t3=False):
        now = datetime.datetime.now(tz=constants.timezone).time()
        def _done(o):
            try:
                return datetime.datetime.strptime(o[4]["st"], "%I:%M %p").time() < now
            except Exception:
                return False
        allm = [o for o in _match_options() if o[1] == is_t3]
        self._opts = ([o for o in allm if not _done(o)] + [o for o in allm if _done(o)])[:25]
        options = [discord.SelectOption(
            label=f"{info.get('st', '?')}  {'T3 ' if t3 else ''}Lobby {lobby} \u00b7 M{m}" + ("  (done)" if _done(o) else ""),
            value=str(k), description=f"{str(info.get('map', '?')).title()}, ID at {info.get('idt', '?')}")
            for k, o in enumerate(self._opts) for (_, t3, lobby, m, info) in [o]]
        super().__init__(placeholder=f"{_reg_kind(is_t3)}: which lobby and match?", options=options or [discord.SelectOption(label="No matches scheduled", value="none")], disabled=not options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await _safe_reply(interaction, "No matches scheduled.", delete_after=15)
        _, is_t3, lobby, m, info = self._opts[int(self.values[0])]
        await interaction.response.send_modal(RoomIdModal(is_t3, lobby, m, info))

class RoomIdView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(RoomMatchSelect(False))
        self.add_item(RoomMatchSelect(True))

MOD_PANEL_FILE = "mod_panel.json"

def _load_mod_panel_ref():
    try:
        with open(MOD_PANEL_FILE, "r") as f:
            ref = json.load(f)
        return {"channel_id": int(ref["channel_id"]), "message_id": int(ref["message_id"])}
    except Exception:
        return None

def _save_mod_panel_ref(channel_id, message_id):
    _atomic_write_json(MOD_PANEL_FILE, {"channel_id": channel_id, "message_id": message_id})

async def restore_mod_panel():
    """On startup: refresh the saved panel (latest text + buttons), or post it
    again in the same channel if someone deleted it. The buttons themselves are
    persistent views, so even an old panel message keeps working."""
    ref = _load_mod_panel_ref()
    if not ref:
        return
    channel = bot.get_channel(ref["channel_id"])
    if channel is None:
        print("[modpanel] saved channel not found; run /modpanel again")
        return
    try:
        msg = await channel.fetch_message(ref["message_id"])
        await msg.edit(embed=mod_panel_embed(), view=ModPanelView())
        print("[modpanel] restored")
    except discord.NotFound:
        try:
            msg = await channel.send(embed=mod_panel_embed(), view=ModPanelView())
            _save_mod_panel_ref(channel.id, msg.id)
            print("[modpanel] panel was deleted; posted a new one")
        except Exception as e:
            print(f"[modpanel] repost failed (non-fatal): {e}")
    except Exception as e:
        print(f"[modpanel] restore failed (non-fatal): {e}")

def _next_room_ids(limit=2):
    """Room IDs still to post today, earliest ID time first."""
    now = datetime.datetime.now(tz=constants.timezone)
    out = []
    for st, is_t3, lobby, m, info in _match_options():
        try:
            idt = datetime.datetime.strptime(info["idt"], "%I:%M %p")
            start = datetime.datetime.strptime(info["st"], "%I:%M %p")
        except Exception:
            continue
        if now.time() <= start.time():
            out.append((idt.time(), is_t3, lobby, m, info))
    out.sort(key=lambda o: o[0])
    return out[:limit]

def mod_panel_embed():
    e = discord.Embed(
        title="Mod Panel",
        description="Pick a lobby below to open its desk. Replies are only visible to you.",
        color=0x229db7)
    for is_t3 in (False, True):
        e.add_field(name=f"{_reg_kind(is_t3)} registration", value=_reg_summary_line(is_t3), inline=True)
    nxt = _next_room_ids()
    if nxt:
        lines = [f"{_reg_kind(t3)} L{lobby} M{m} {info.get('map', '').title()}  \u00b7  ID {_when(_ist_today(info.get('idt')))}" for _, t3, lobby, m, info in nxt]
        e.add_field(name="Next room IDs", value="\n".join(lines), inline=False)
    flows = len(constants.running_processes)
    if flows:
        e.add_field(name="Enrolling right now", value=str(flows), inline=True)
    e.set_footer(text=f"Updated {datetime.datetime.now(tz=constants.timezone).strftime('%I:%M %p')} IST")
    return e

_panel_msg = {"msg": None, "sig": None}

@tasks.loop(minutes=1)
async def mod_panel_refresher():
    """Keep the panel's summary current without anyone pressing a button.
    Edits only when something visible changed, so it stays well under limits."""
    try:
        ref = _load_mod_panel_ref()
        if not ref:
            return
        e = mod_panel_embed()
        sig = (e.description, tuple((f.name, f.value) for f in e.fields))
        if sig == _panel_msg["sig"]:
            return
        msg = _panel_msg["msg"]
        if msg is None or msg.id != ref["message_id"]:
            channel = bot.get_channel(ref["channel_id"])
            if channel is None:
                return
            msg = await channel.fetch_message(ref["message_id"])
            _panel_msg["msg"] = msg
        await msg.edit(embed=e)
        _panel_msg["sig"] = sig
    except discord.NotFound:
        _panel_msg["msg"] = None
    except Exception as ex:
        print(f"[modpanel] refresh failed (non-fatal): {ex}")


class ModPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PanelDeskSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _has_bot_access(interaction.user):
            return True
        await _safe_reply(interaction, "This panel is for mods only.", delete_after=15)
        return False

    @discord.ui.button(label="Live status", emoji="\U0001f4ca", style=discord.ButtonStyle.primary, custom_id="modpanel:status", row=1)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _safe_reply(interaction, embed=build_mod_status_embed(), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Find team", emoji="\U0001f50e", style=discord.ButtonStyle.primary, custom_id="modpanel:find", row=1)
    async def find(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FindTeamModal())

    @discord.ui.button(label="Room ID", emoji="\U0001f3ae", style=discord.ButtonStyle.primary, custom_id="modpanel:room", row=1)
    async def room(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _safe_reply(interaction, "Next ones first:", view=RoomIdView(), delete_after=300)

    @discord.ui.button(label="Lift ban", emoji="\U0001f513", style=discord.ButtonStyle.secondary, custom_id="modpanel:lift", row=1)
    async def lift(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _safe_reply(interaction, "Lift a ban, cooldown or blacklist:", view=LiftView(), delete_after=300)

    @discord.ui.button(label="Backup", emoji="\U0001f4be", style=discord.ButtonStyle.secondary, custom_id="modpanel:backup", row=2)
    async def backup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        stamp = datetime.datetime.now(tz=constants.timezone).strftime("%Y%m%d-%H%M%S")
        state_snapshot.save_state_snapshot()
        state_snapshot.save_state_snapshot(suffix=f"manual-{stamp}")
        path = state_snapshot._snapshot_path(f"manual-{stamp}")
        try:
            await interaction.followup.send(f"Backup saved ({stamp}).", file=discord.File(path), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Backup saved on the server, but sending the file failed: {e}", ephemeral=True)

    @discord.ui.button(label="Clean threads", emoji="\U0001f9f9", style=discord.ButtonStyle.secondary, custom_id="modpanel:sweep", row=2)
    async def sweep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted, kept = await sweep_flow_threads(min_idle_minutes=10, reason=f"panel by {interaction.user.name}")
        await interaction.followup.send(
            f"Removed {deleted} leftover enroll/update/delete thread(s). Kept {kept} still in use or used in the last 10 min.",
            ephemeral=True)

    @discord.ui.button(label="Close Open reg", emoji="\u23f9\ufe0f", style=discord.ButtonStyle.danger, custom_id="modpanel:close_open", row=2)
    async def close_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _reg_open(False):
            return await _safe_reply(interaction, "Open registration is already closed.", delete_after=20)
        await _safe_reply(interaction, f"Close Open registration now?\n{_reg_status_block(False)}", view=ConfirmCloseView(False), delete_after=60)

    @discord.ui.button(label="Close T3 reg", emoji="\u23f9\ufe0f", style=discord.ButtonStyle.danger, custom_id="modpanel:close_t3", row=2)
    async def close_t3(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _reg_open(True):
            return await _safe_reply(interaction, "T3 registration is already closed.", delete_after=20)
        await _safe_reply(interaction, f"Close T3 registration now?\n{_reg_status_block(True)}", view=ConfirmCloseView(True), delete_after=60)

# ──────────────────────────────────────────────────────────────────
# Cancel / Claim Slot System
# ──────────────────────────────────────────────────────────────────

def get_group_for_lobby(lobby_number, is_t3=False):
    """Reverse-lookup: given a lobby number, find which group it belongs to."""
    lobby_map = constants.GROUP_LOBBY_MAP2 if is_t3 else constants.GROUP_LOBBY_MAP
    for group, lobbies in lobby_map.items():
        if lobby_number in lobbies:
            return group
    return None

def is_before_cancel_deadline(group, is_t3=False):
    """Legacy group-based check. Kept as fallback; prefer per-lobby check below."""
    deadlines = constants.CANCEL_DEADLINES_T3 if is_t3 else constants.CANCEL_DEADLINES
    if group not in deadlines:
        return False
    deadline_str = deadlines[group]  # e.g. "13:30"
    hour, minute = map(int, deadline_str.split(":"))
    now = datetime.datetime.now(tz=constants.timezone)
    deadline_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now < deadline_time

def get_lobby_m1_start(lobby_number, is_t3=False):
    """M1 START for this lobby today (IST). None if lobby not in schedule."""
    try:
        sched = constants.match_schedule_t3 if is_t3 else constants.match_schedule
        start_str = sched[int(lobby_number)][1]["st"]  # e.g. "3:07 PM"
        now = datetime.datetime.now(tz=constants.timezone)
        parsed = datetime.datetime.strptime(start_str, "%I:%M %p")
        return now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    except Exception:
        return None

def get_lobby_cancel_deadline(lobby_number, is_t3=False):
    """Cancel deadline = M1 START minus 30 mins (IST). None if unknown."""
    start = get_lobby_m1_start(lobby_number, is_t3)
    if start is None:
        return None
    return start - datetime.timedelta(minutes=30)

def is_before_lobby_cancel_deadline(lobby_number, is_t3=False):
    """True if now is before this lobby's 30-mins-before-M1 deadline."""
    deadline = get_lobby_cancel_deadline(lobby_number, is_t3)
    if deadline is None:
        group = get_group_for_lobby(lobby_number, is_t3)
        return is_before_cancel_deadline(group, is_t3)
    return datetime.datetime.now(tz=constants.timezone) < deadline

def is_lobby_started(lobby_number, is_t3=False):
    """True if this lobby's M1 START has passed today (IST)."""
    start = get_lobby_m1_start(lobby_number, is_t3)
    if start is None:
        return False
    return datetime.datetime.now(tz=constants.timezone) >= start

def _fmt_ist(dt):
    try:
        return dt.strftime("%I:%M %p")
    except Exception:
        return "unknown"

def _ts(dt, style="t"):
    """Discord timestamp. Shows in each reader's own clock; style R counts down live."""
    try:
        return f"<t:{int(dt.timestamp())}:{style}>"
    except Exception:
        return "unknown"

def _when(dt):
    """'3:05 PM (in 12 minutes)', kept up to date by Discord itself."""
    if dt is None:
        return "unknown"
    return f"{_ts(dt, 't')} ({_ts(dt, 'R')})"

def _ist_today(time_str):
    """'3:05 PM' -> today at that time in IST, or None."""
    try:
        parsed = datetime.datetime.strptime(str(time_str).strip().upper(), "%I:%M %p")
    except Exception:
        return None
    now = datetime.datetime.now(tz=constants.timezone)
    return now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)

def _next_reg_open():
    """Next 12 PM IST on a scrim day (registration opens then)."""
    now = datetime.datetime.now(tz=constants.timezone)
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    for add in range(8):
        day = noon + datetime.timedelta(days=add)
        if day > now and day.weekday() in constants.days_to_run:
            return day
    return None

def _match_lines(matches):
    """One line per match: map, when the ID drops, when it starts."""
    out = []
    for m, info in matches.items():
        idt, st = _ist_today(info.get("idt")), _ist_today(info.get("st"))
        out.append(f"M{m} {info.get('map', '?').title()}  \u00b7  ID {_ts(idt) if idt else info.get('idt', '?')}, start {_ts(st) if st else info.get('st', '?')}")
    return "\n".join(out)

_claim_expiry_tasks = {}

async def _expire_one_claim(slot_key):
    """One-shot expiry for a single claim message. Idempotent; never raises."""
    try:
        data = load_cancelled_slots()
        entry = data.get(slot_key)
        if not isinstance(entry, dict) or entry.get("claimed"):
            return False
        try:
            channel = bot.get_channel(entry.get("channel_id"))
            if channel:
                try:
                    msg = await channel.fetch_message(entry.get("message_id"))
                    await msg.delete()
                except Exception:
                    pass
        except Exception:
            pass
        entry["claimed"] = True
        entry["claimed_by"] = entry.get("claimed_by") or "EXPIRED"
        entry["expired"] = True
        try:
            save_cancelled_slots(data)
        except Exception as e:
            print(f"[expire] save failed: {e}")
        print(f"[expire] expired {slot_key} at match start")
        return True
    except Exception as e:
        print(f"[expire] entry {slot_key} failed: {e}")
        return False
    finally:
        _claim_expiry_tasks.pop(slot_key, None)

async def _expiry_sleeper(slot_key, delay):
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        await _expire_one_claim(slot_key)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[expire] sleeper {slot_key} failed: {e}")

def schedule_claim_expiry(slot_key, lobby_number, is_t3=False):
    """Schedule message deletion exactly at this lobby's M1 START. Never raises."""
    try:
        _cancel_claim_expiry(slot_key)
        start = get_lobby_m1_start(int(lobby_number), is_t3)
        now = datetime.datetime.now(tz=constants.timezone)
        delay = (start - now).total_seconds() if start else 0
        if delay is None or delay < 0:
            try:
                created = int(str(slot_key).rsplit("_", 1)[-1])
                if int(now.timestamp()) - created > 12 * 3600:
                    delay = 0
                else:
                    delay = max(0, delay if isinstance(delay, (int, float)) else 0)
            except Exception:
                delay = max(0, delay if isinstance(delay, (int, float)) else 0)
        task = asyncio.create_task(_expiry_sleeper(slot_key, delay))
        _claim_expiry_tasks[slot_key] = task
    except Exception as e:
        print(f"[expire] schedule failed for {slot_key}: {e}")

def _cancel_claim_expiry(slot_key):
    try:
        task = _claim_expiry_tasks.pop(slot_key, None)
        if task and not task.done():
            task.cancel()
    except Exception:
        pass

def cancel_claim_expiry(slot_key):
    _cancel_claim_expiry(slot_key)

async def expire_stale_claim_messages(reason="startup"):
    """Startup sweep: delete unclaimed messages for lobbies whose M1 already
    started (e.g. bot was down at start time). Never raises.
    """
    try:
        data = load_cancelled_slots()
    except Exception as e:
        print(f"[expire] load failed ({reason}): {e}")
        return 0
    changed = False
    expired = 0
    now_ts = int(datetime.datetime.now(tz=constants.timezone).timestamp())
    for key, entry in list(data.items()):
        try:
            if not isinstance(entry, dict) or entry.get("claimed"):
                continue
            lobby = int(entry.get("lobby"))
            is_t3 = entry.get("type") == "t3"
            started = is_lobby_started(lobby, is_t3)
            if not started:
                # safety net: entries older than 12h are dead even across midnight
                try:
                    created = int(str(key).rsplit("_", 1)[-1])
                    if now_ts - created < 12 * 3600:
                        continue
                except Exception:
                    continue
                started = True
            if not started:
                continue
            # delete the discord message if it still exists
            try:
                channel = bot.get_channel(entry.get("channel_id"))
                if channel:
                    try:
                        msg = await channel.fetch_message(entry.get("message_id"))
                        await msg.delete()
                    except Exception:
                        pass
            except Exception:
                pass
            entry["claimed"] = True
            entry["claimed_by"] = entry.get("claimed_by") or "EXPIRED"
            entry["expired"] = True
            changed = True
            expired += 1
        except Exception as e:
            print(f"[expire] entry {key} failed: {e}")
            continue
    if changed:
        try:
            save_cancelled_slots(data)
        except Exception as e:
            print(f"[expire] save failed: {e}")
    if expired:
        print(f"[expire] expired {expired} claim message(s) ({reason})")
    return expired

def load_cancelled_slots():
    """Load cancelled_slots.json from disk. Returns dict."""
    try:
        with open('cancelled_slots.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_cancelled_slots(data):
    """Save cancelled_slots.json to disk."""
    _atomic_write_json('cancelled_slots.json', data)

async def update_slot_list_after_cancel(lobby_number, is_t3):
    """
    Re-read the lobby JSON file and update the slot list embed in the IDP channel.
    Uses lobby_details.json / lobby_details2.json to find the message to edit.
    """
    json_file_name = f"alt_lobby_{lobby_number}_teams.json" if is_t3 else f"lobby_{lobby_number}_teams.json"

    try:
        with open(json_file_name, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"{json_file_name} not found, can't update slot list.")
        return

    team_names = list(data.keys())
    # find the IDP channel for this lobby
    if is_t3:
        idp_channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"t3-idp-{lobby_number}")
    else:
        idp_channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"group-{lobby_number}-idp")

    if idp_channel:
        await send_slots_list(team_names, lobby_number, idp_channel, edit_slots_list=True, use_alt_lobby=is_t3, cancel_disabled=False)

async def refresh_live_slot_list(lobby_number, is_t3=False):
    """Live slot list update after each booking. Creates the list on first
    booking, edits it after that. Cancel stays disabled until close.
    Reads teams from memory (authoritative during registration). Never raises.
    """
    try:
        if is_t3:
            idx = int(lobby_number) - 1
            if idx < 0 or idx >= len(constants.special_lobby_teams):
                return
            team_names = list(constants.special_lobby_teams[idx].keys())
            channel_name = f"t3-idp-{lobby_number}"
            use_alt = True
        else:
            idx = int(lobby_number) - 1
            if idx < 0 or idx >= len(constants.lobby_teams):
                return
            team_names = list(constants.lobby_teams[idx].keys())
            channel_name = f"group-{lobby_number}-idp"
            use_alt = False
        idp_channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=channel_name)
        if not idp_channel:
            return
        await send_slots_list(
            team_names, int(lobby_number), idp_channel,
            edit_slots_list=True, use_alt_lobby=use_alt, cancel_disabled=True,
        )
    except Exception as e:
        print(f"[live-slots] refresh failed lobby {lobby_number} (non-fatal): {e}")
    try:
        await refresh_reg_message(is_t3=use_alt, show_slots=True)
    except Exception as e:
        print('[reg-slots] refresh failed (non-fatal):', e)


class CancelSlotButton(discord.ui.Button):
    """Button shown in IDP channels - lets a team cancel their slot."""
    def __init__(self, disabled=False):
        super().__init__(label='Cancel Slot', style=discord.ButtonStyle.danger, row=1, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        channel_name = interaction.channel.name

        # detect open vs t3 from channel name
        is_t3 = channel_name.startswith('t3-idp-')
        if is_t3:
            lobby_number = int(channel_name.split('-')[-1])
        else:
            lobby_number = int(channel_name.split('-')[1])

        # find which group this lobby belongs to
        group = get_group_for_lobby(lobby_number, is_t3)
        if not group:
            return await interaction.response.send_message(
                "Could not determine which group this lobby belongs to.", ephemeral=True, delete_after=15
            )

        # check per-lobby deadline: 30 mins before M1 start
        if is_lobby_started(lobby_number, is_t3):
            return await interaction.response.send_message(
                f"Match for Lobby {lobby_number} has already started. Cancel closed.",
                ephemeral=True, delete_after=30
            )
        if not is_before_lobby_cancel_deadline(lobby_number, is_t3):
            dl = get_lobby_cancel_deadline(lobby_number, is_t3)
            st = get_lobby_m1_start(lobby_number, is_t3)
            return await interaction.response.send_message(
                f"Cancel closed for Lobby {lobby_number} at {_ts(dl)} "
                f"(30 minutes before Match 1 at {_ts(st)}).",
                ephemeral=True, delete_after=30
            )

        # find the team in the lobby JSON
        json_file_name = f"alt_lobby_{lobby_number}_teams.json" if is_t3 else f"lobby_{lobby_number}_teams.json"
        try:
            with open(json_file_name, 'r') as f:
                lobby_data = json.load(f)
        except FileNotFoundError:
            return await interaction.response.send_message("Lobby data not found.", ephemeral=True, delete_after=15)

        team_name = _team_in_lobby_for(user, lobby_data, lobby_number, is_t3)
        if not team_name:
            return await interaction.response.send_message(
                "Only your team's booker, or the teammate holding this lobby's role, can cancel. "
                "Tap My status in the registration channel to see where your team is.",
                ephemeral=True, delete_after=30)

        slot_no = list(lobby_data.keys()).index(team_name) + 3
        dl = get_lobby_cancel_deadline(lobby_number, is_t3)
        await interaction.response.send_message(
            f"Cancel {team_name}'s slot {slot_no:02} in {_reg_kind(is_t3)} Lobby {lobby_number}?\n"
            f"Another team can claim it right away and you can't get it back. Cancelling closes {_when(dl)}.",
            view=CancelSlotConfirmView(team_name, lobby_number, group, is_t3), ephemeral=True)


def _team_in_lobby_for(user, lobby_data, lobby_number, is_t3):
    """The team in this lobby that `user` may cancel for: they booked it, or
    they're on that team and hold the lobby's IDP role (after a transfer)."""
    for tn, uid in lobby_data.items():
        if str(uid).isdigit() and int(uid) == user.id:
            return tn
    holds_role = any(r.name == _lobby_role_name(lobby_number, is_t3) for r in getattr(user, "roles", []))
    if holds_role:
        for tn, uid in lobby_data.items():
            if str(uid).isdigit() and str(user.id) in _team_member_ids(tn):
                return tn
    return None


class CancelSlotConfirmView(discord.ui.View):
    """One-tap confirm for a player's cancel (replaces typing the team name)."""
    def __init__(self, team_name, lobby_number, group, is_t3):
        super().__init__(timeout=120)
        self.team_name, self.lobby_number, self.group, self.is_t3 = team_name, lobby_number, group, is_t3

    @discord.ui.button(label="Yes, cancel our slot", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelling...", view=None)
        try:
            ok, msg = await cancel_team_slot(self.team_name, self.lobby_number, self.is_t3, post_claim=True,
                                             enforce_deadline=True, by_label=interaction.user.display_name)
        except Exception as e:
            print(f"[cancel] {e}")
            ok, msg = False, "Something went wrong while cancelling. Tap My status to check, then try again."
        if ok:
            msg = (f"\u2705 Cancelled. {self.team_name} is out of {_reg_kind(self.is_t3)} Lobby {self.lobby_number}, "
                   "and the slot is up for claim in the registration channel.")
        await interaction.edit_original_response(content=msg)

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="OK, your slot is kept.", view=None)


async def cancel_team_slot(team_name, lobby_number, is_t3, post_claim=True, enforce_deadline=False, by_label=""):
    """Free a team's slot: CANCELLED placeholder in the same position, IDP role
    taken from the booker, slot list updated, and (optionally) a claim message
    posted. Used by a team's own Cancel Slot and by a mod's Remove team.
    Returns (ok, message)."""
    group = get_group_for_lobby(lobby_number, is_t3)
    async with constants.cancel_slots_lock:
        if enforce_deadline:
            if is_lobby_started(lobby_number, is_t3):
                return False, f"Match for Lobby {lobby_number} has already started. Cancel closed."
            if not is_before_lobby_cancel_deadline(lobby_number, is_t3):
                dl = get_lobby_cancel_deadline(lobby_number, is_t3)
                return False, f"Cancel closed for Lobby {lobby_number} at {_ts(dl)}."
        json_file_name = f"alt_lobby_{lobby_number}_teams.json" if is_t3 else f"lobby_{lobby_number}_teams.json"
        try:
            with open(json_file_name, 'r') as f:
                lobby_data = json.load(f)
        except FileNotFoundError:
            return False, "Lobby data not found."
        if team_name not in lobby_data:
            return False, f"{team_name} is not in this lobby anymore."
        booker_id = lobby_data[team_name]

        # keep positions: replace the team with a CANCELLED placeholder
        cancel_key = "CANCELLED"
        counter = 1
        while cancel_key in lobby_data:
            counter += 1
            cancel_key = f"CANCELLED ({counter})"
        items = list(lobby_data.items())
        position = next(i for i, (k, v) in enumerate(items) if k == team_name)
        items[position] = (cancel_key, "cancelled")
        _atomic_write_json(json_file_name, dict(items))

        guild = bot.get_guild(constants.GUILD_ID)
        role = discord.utils.get(guild.roles, name=f"T3 G{lobby_number} IDP" if is_t3 else f"Group {lobby_number} IDP")
        if role:
            # The booker may have passed the role to a teammate, so check the whole team.
            ids = _team_member_ids(team_name) | ({str(booker_id)} if str(booker_id).isdigit() else set())
            holders = {m.id: m for m in role.members if str(m.id) in ids}
            booker = guild.get_member(int(booker_id)) if str(booker_id).isdigit() else None
            if booker and role in booker.roles:
                holders[booker.id] = booker
            for member in holders.values():
                try:
                    await member.remove_roles(role)
                except Exception as e:
                    print(f"Error removing IDP role: {e}")

        await update_slot_list_after_cancel(lobby_number, is_t3)

        if post_claim:
            claim_embed = discord.Embed(
                title=f"Slot open  \u00b7  Group {group}, Lobby {lobby_number}",
                description=f"{team_name} gave up their slot. The first team to claim it gets it.",
                color=0x2ecc71)
            claim_embed.add_field(name="Registration", value=_reg_kind(is_t3), inline=True)
            start = get_lobby_m1_start(lobby_number, is_t3)
            if start:
                claim_embed.add_field(name="Match 1", value=_when(start), inline=True)
            if by_label:
                claim_embed.set_footer(text=f"Freed by {by_label}")
            reg_channel_id = constants.SPECIAL_REGISTRATION_CHANNEL_ID if is_t3 else constants.REGISTRATION_CHANNEL_ID
            claim_message = await bot.get_channel(reg_channel_id).send(embed=claim_embed, view=ClaimSlotView(lobby_number, group, is_t3))
            cancelled_data = load_cancelled_slots()
            slot_key = f"{lobby_number}_{group}_{int(datetime.datetime.now(tz=constants.timezone).timestamp())}"
            cancelled_data[slot_key] = {
                "team_name": team_name, "lobby": lobby_number, "group": group,
                "type": "t3" if is_t3 else "open", "message_id": claim_message.id,
                "channel_id": reg_channel_id, "claimed": False, "cancel_key": cancel_key,
            }
            save_cancelled_slots(cancelled_data)
            state_snapshot.save_state_snapshot()
            try:
                schedule_claim_expiry(slot_key, lobby_number, is_t3)
            except Exception as e:
                print(f"[expire] schedule failed (non-fatal): {e}")
            return True, f"\u2705 {team_name} removed from {_reg_kind(is_t3)} Lobby {lobby_number}. A claim message is up in the registration channel."
        state_snapshot.save_state_snapshot()
        return True, f"\u2705 {team_name} removed from {_reg_kind(is_t3)} Lobby {lobby_number}. The slot shows as CANCELLED."

class ClaimSlotView(discord.ui.View):
    """Persistent view with a Claim button - attached to the claim message in registration channel."""
    def __init__(self, lobby_number, group, is_t3):
        super().__init__(timeout=None)
        self.add_item(ClaimSlotButton(lobby_number, group, is_t3))


class ClaimSlotButton(discord.ui.Button):
    """Button to claim a cancelled slot. Uses custom_id for restart persistence."""
    def __init__(self, lobby_number, group, is_t3):
        reg_type = "t3" if is_t3 else "open"
        custom_id = f"claim_{reg_type}_{group}_{lobby_number}_{int(datetime.datetime.now(tz=constants.timezone).timestamp())}"
        super().__init__(
            label=f'Claim Slot - Group {group}, Lobby {lobby_number}',
            style=discord.ButtonStyle.green,
            custom_id=custom_id
        )
        self.lobby_number = lobby_number
        self.group = group
        self.is_t3 = is_t3

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        team_name = await validate_registration(user)
        reason = await _booking_block_reason(user, team_name, False)
        if reason:
            return await interaction.response.send_message(reason, ephemeral=True, delete_after=60)
        # No time check for claims: if the message is there and the click is
        # valid, the team gets the slot. Expiry deletes the message at M1 start.
        label = f"{'T3 ' if self.is_t3 else ''}Group {self.group}, Lobby {self.lobby_number}"
        await interaction.response.send_message(
            f"Claim {label} for {team_name}? This counts as today's booking for your team.",
            view=ClaimConfirmView(self.lobby_number, self.group, self.is_t3, team_name, interaction.message),
            ephemeral=True, delete_after=90)

class ClaimConfirmView(discord.ui.View):
    """One-tap confirm for a claim (replaces typing "confirm" into a modal)."""
    def __init__(self, lobby_number, group, is_t3, team_name, claim_message):
        super().__init__(timeout=90)
        self.lobby_number = lobby_number
        self.group = group
        self.is_t3 = is_t3
        self.team_name = team_name
        self.claim_message = claim_message

    @discord.ui.button(label="Claim it", style=discord.ButtonStyle.success, emoji="\u2705")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(content="Claiming...", view=None)
        except Exception:
            await interaction.response.defer(ephemeral=True)
        ok, msg = await claim_cancelled_slot(interaction.user, self.lobby_number, self.group, self.is_t3,
                                             self.team_name, self.claim_message)
        try:
            await interaction.edit_original_response(content=msg, view=None)
        except Exception:
            await _safe_reply(interaction, msg)

    @discord.ui.button(label="Not now", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="OK, not claimed.", view=None)

async def claim_cancelled_slot(member, lobby_number, group, is_t3, team_name, claim_message):
    """Give a cancelled slot to team_name. Returns (ok, message for the user)."""
    async with constants.cancel_slots_lock:
        cancelled_data = load_cancelled_slots()
        slot_entry = None
        slot_key = None
        for key, entry in cancelled_data.items():
            if (entry["lobby"] == lobby_number
                and entry["group"] == group
                and entry["type"] == ("t3" if is_t3 else "open")
                and entry["message_id"] == claim_message.id
                and not entry["claimed"]):
                slot_entry = entry
                slot_key = key
                break
        if not slot_entry:
            return False, "This slot has already been claimed by someone else."

        # enforce the per-day limit: a team at the limit can't claim more
        if is_t3:
            if (team_name, group) in constants.special_registered_set:
                return False, "Your team is already registered in this group."
            if _groups_booked(team_name, True) >= constants.MAX_T3_GROUP_REGISTRATIONS:
                return False, "Your team is already registered in 1 lobby (1 lobby max per day)."
        else:
            if any(entry[0] == team_name for entry in constants.registered_set):
                return False, "Your team is already registered in another lobby (1 lobby max per day)."

        json_file_name = f"alt_lobby_{lobby_number}_teams.json" if is_t3 else f"lobby_{lobby_number}_teams.json"
        try:
            with open(json_file_name, 'r') as f:
                lobby_data = json.load(f)
        except FileNotFoundError:
            return False, "Lobby data not found."

        # replace the CANCELLED placeholder with the claiming team at the same position
        cancel_key = slot_entry.get("cancel_key")
        if cancel_key and cancel_key in lobby_data:
            items = list(lobby_data.items())
            position = next(i for i, (k, v) in enumerate(items) if k == cancel_key)
            items[position] = (team_name, member.id)
            lobby_data = dict(items)
        else:
            lobby_data[team_name] = member.id
        _atomic_write_json(json_file_name, lobby_data)
        slot_no = list(lobby_data.keys()).index(team_name) + 3

        role_name = f"T3 G{lobby_number} IDP" if is_t3 else f"Group {lobby_number} IDP"
        role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=role_name)
        if role:
            try:
                await member.add_roles(role)
            except Exception as e:
                print(f"Error assigning IDP role: {e}")

        await update_slot_list_after_cancel(lobby_number, is_t3)

        cancelled_data[slot_key]["claimed"] = True
        cancelled_data[slot_key]["claimed_by"] = team_name
        save_cancelled_slots(cancelled_data)
        # Count the claim as a registration so the same team can't claim a
        # second cancelled slot (the per-day limit checks these sets).
        _reg_set(is_t3).add((team_name, group))
        cancel_claim_expiry(slot_key)
        state_snapshot.save_state_snapshot()
        try:
            await claim_message.delete()
        except Exception as e:
            print(f"Error deleting claim message: {e}")

    return True, (f"\u2705 Slot claimed! {team_name} is in Group {group}, Lobby {lobby_number}, slot {slot_no:02}.\n"
                  f"Your slot list is in {_idp_channel_name(lobby_number, is_t3)}.")

class PlayerSelectView(discord.ui.View):
    def __init__(self, row, role, giver_id=None):
        super().__init__(timeout=300)
        self.add_item(PlayerSelectDropdown(row, role, giver_id))

class AmateurDetailsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Submit Team Details", style=discord.ButtonStyle.green)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AmateurDetailsModal())

class AmateurDetailsView(discord.ui.View):
    def __init__(self,disabled = False):
        super().__init__(timeout=None)
        self.add_item(AmateurDetailsButton())

class AmateurDetailsModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Amateur Team Details!")
        
        # Adding input fields for old and new team names
        self.team_name_input = discord.ui.TextInput(
            label="Team Name", required=True
        )
        self.add_item(self.team_name_input)
        
        self.number_input = discord.ui.TextInput(
            label="Whatsapp no. to add in Trident Scrims Group", required=True
        )
        self.add_item(self.number_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        # Extract the details from the form inputs
        team_name = self.team_name_input.value
        number = self.number_input.value
        user = interaction.user
        user_id = user.id

        # Construct the message to send to the mod channel
        message_content = (
            f"Submitted by {user.mention}\n"
            f"Team Name: {team_name}\n"
            f"Number: {number}"
        )

        # Send the details to the mod channel
        mod_channel = await interaction.client.fetch_channel(constants.STAFF_CHANNEL_ID)
        await mod_channel.send(message_content)

        # Acknowledge the submission to the user
        await interaction.response.send_message(
            f"Your details have been submitted! Please wait atleast 24 hours and you will be added to the group.",
            ephemeral=True,delete_after=120
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await _safe_reply(interaction, "An error occurred while processing your request. Please try again later.", delete_after=240)
        print(f"An error occurred during the team name change request for {interaction.user}: {error}")

def _captcha_problem(word, sum1, sum2, q=None):
    """None if the booking captcha is right, else a short line saying which part is off."""
    q = q or constants.captcha_question_variables
    if str(word).strip().lower() != str(q[0]).lower():
        return f"The word didn't match (it's `{q[0]}`)."
    try:
        a, b = int(str(sum1).strip()), int(str(sum2).strip())
    except ValueError:
        return "The sums take numbers only."
    if a != int(q[1]) + int(q[2]):
        return f"{q[1]} + {q[2]} isn't {a}."
    if b != int(q[3]) + int(q[4]):
        return f"{q[3]} + {q[4]} isn't {b}."
    return None

async def validate_captcha(captcha_phrase : str, sum1_answer : int, sum2_answer : int):
    # Placeholder for actual captcha validation logic
    return (captcha_phrase.lower().rstrip() == constants.captcha_question_variables[0] and sum1_answer == int(constants.captcha_question_variables[1] + constants.captcha_question_variables[2]) and sum2_answer == int(constants.captcha_question_variables[3] + constants.captcha_question_variables[4]))  # Replace with actual validation
    
# Event handler for when the bot is ready

# ---------------------------------------------------------------------------
# Leftover flow threads (enroll/update/delete) are cleaned up on their own:
# once in the background after boot, then every 30 minutes. A thread is only
# removed if nobody is using it (no open flow points at it) and it has been
# quiet for a while, so a live enrollment is never touched.
# ---------------------------------------------------------------------------
_FLOW_THREAD_SUFFIXES = ("-enroll", "-update", "-delete")

async def sweep_flow_threads(min_idle_minutes=20, reason="sweep"):
    """Delete idle, ownerless flow threads in the enrollment channel. Never raises.
    Returns (deleted, kept)."""
    deleted = kept = 0
    try:
        guild = bot.get_guild(constants.GUILD_ID)
        channel = guild.get_channel(constants.ENROLLMENT_CHANNEL_ID) if guild else None
        if channel is None:
            return 0, 0
        live = {e.get("thread_id") for e in constants.running_processes.values() if isinstance(e, dict)}
        cutoff = discord.utils.utcnow() - datetime.timedelta(minutes=min_idle_minutes)
        seen, threads = set(), []
        for t in list(getattr(channel, "threads", []) or []):
            if t.id not in seen:
                seen.add(t.id); threads.append(t)
        for private in (True, False):
            try:
                async for t in channel.archived_threads(limit=100, private=private):
                    if t.id not in seen:
                        seen.add(t.id); threads.append(t)
            except Exception as e:
                print(f"[thread-sweep] archived fetch failed (private={private}): {e}")
        for t in threads:
            try:
                if not t.name.endswith(_FLOW_THREAD_SUFFIXES) or t.id in live:
                    continue
                last = None
                async for m in t.history(limit=1):
                    last = m.created_at
                last = last or getattr(t, "created_at", None)
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=datetime.timezone.utc)
                if last is not None and last > cutoff:
                    kept += 1
                    continue
                await t.delete()
                deleted += 1
                await asyncio.sleep(1.2)  # gentle on rate limits; this runs in the background
            except Exception as e:
                print(f"[thread-sweep] {getattr(t, 'name', '?')}: {e}")
        if deleted:
            print(f"[thread-sweep] {reason}: deleted {deleted} leftover thread(s), kept {kept}")
    except Exception as e:
        print(f"[thread-sweep] failed ({reason}): {e}")
    return deleted, kept

@tasks.loop(minutes=30)
async def flow_thread_sweeper():
    if flow_thread_sweeper.current_loop == 0:
        return  # the boot sweep covers startup
    await sweep_flow_threads(min_idle_minutes=20, reason="periodic")

async def _boot_thread_sweep():
    # After a restart no flow is running, so every old flow thread is dead.
    # Wait a minute so startup (menus, IDP views) finishes first.
    await asyncio.sleep(60)
    await sweep_flow_threads(min_idle_minutes=10, reason="boot")

_setup_done = False
_snapshot_loaded = False

@bot.event
async def on_ready():
    global _setup_done, _snapshot_loaded
    if _setup_done:
        # Gateway reconnect: views, tasks and state are already live. Re-running
        # setup would reload the snapshot over live bookings and raise
        # "Task is already launched" on the loops below.
        print(f"Reconnected as {bot.user}; setup already done, skipping.")
        return

    print(f"We have logged in as {bot.user} but wait we ain't ready")

    if not _snapshot_loaded:
        _snapshot_loaded = True
        try:
            state_snapshot.load_state_snapshot()
        except Exception as e:
            print(f"[snapshot] restore in on_ready failed (non-fatal): {e}")
        try:
            bot.add_view(ModPanelView())  # persistent mod panel buttons survive restarts
        except Exception as e:
            print(f"[modpanel] add_view failed (non-fatal): {e}")

    await bot.tree.sync()  # For both text and slash commands

    if constants.ENROLLMENT_MESSAGE_ID and bot.get_channel(constants.ENROLLMENT_CHANNEL_ID):
        try:
            # Fetch the message
            message = await bot.get_channel(constants.ENROLLMENT_CHANNEL_ID).fetch_message(constants.ENROLLMENT_MESSAGE_ID)
        except discord.NotFound:
            # If the message is not found, handle the case gracefully
            message = None

        if message:
            # Edit the existing message with the dropdown menu (and current help text)
            await message.edit(embed=enrollment_embed(), view=TournamentView())
        else:
            # Send a new message with the dropdown menu
            message = await send_selectmenu(bot.get_channel(constants.ENROLLMENT_CHANNEL_ID))
        
        # Update the interaction message ID
        constants.ENROLLMENT_MESSAGE_ID = message.id

    else:
        # If the channel or message ID is not valid, send the select menu
        message = await send_selectmenu(bot.get_channel(constants.ENROLLMENT_CHANNEL_ID))
        constants.ENROLLMENT_MESSAGE_ID = message.id


    if constants.SPECIAL_REG_MESSAGE_ID and bot.get_channel(constants.SPECIAL_REGISTRATION_CHANNEL_ID):
        try:
            message = await bot.get_channel(
                constants.SPECIAL_REGISTRATION_CHANNEL_ID
            ).fetch_message(constants.SPECIAL_REG_MESSAGE_ID)
        except discord.NotFound:
            message = None

        if message:
            await message.edit(view=RegistrationView3())
        else:
            message = await send_remenu2(
                bot.get_channel(constants.SPECIAL_REGISTRATION_CHANNEL_ID)
            )

        constants.SPECIAL_REG_MESSAGE_ID = message.id

    else:
        message = await send_remenu2(
            bot.get_channel(constants.SPECIAL_REGISTRATION_CHANNEL_ID)
        )
        constants.SPECIAL_REG_MESSAGE_ID = message.id

    if constants.REG_MESSAGE_ID and bot.get_channel(constants.REGISTRATION_CHANNEL_ID):
        try:
            # Fetch the message
            message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
        except discord.NotFound:
            # If the message is not found, handle the case gracefully
            message = None

        if message:
            # Edit the existing message with the dropdown menu
            await message.edit(view=RegistrationView2())
        else:
            # Send a new message with the dropdown menu
            message = await send_remenu(bot.get_channel(constants.REGISTRATION_CHANNEL_ID))
        
        # Update the interaction message ID
        constants.REG_MESSAGE_ID = message.id

    else:
        # If the channel or message ID is not valid, send the select menu
        message = await send_remenu(bot.get_channel(constants.REGISTRATION_CHANNEL_ID))
        constants.REG_MESSAGE_ID = message.id

    try:
        await restore_mod_panel()
    except Exception as e:
        print(f"[modpanel] restore failed (non-fatal): {e}")

    # Re-attach the info and FAQ buttons; they were never re-attached after a
    # restart, so "Tier-3 Schedule" (linked from every slot list) stopped working.
    for _cid, _mid, _view in ((constants.INFO_CHANNEL_ID, constants.SCRIMS_INFO_MESSAGE_ID, ScrimsOverviewView),
                              (constants.HOW_TO_PLAY_CHANNEL_ID, constants.FAQ_MESSAGE_ID, FaqView)):
        try:
            _ch = bot.get_channel(_cid)
            if _ch and _mid:
                _msg = await _ch.fetch_message(_mid)
                await _msg.edit(view=_view())
        except Exception as e:
            print(f"[views] could not re-attach {_view.__name__} (non-fatal): {e}")

    # Re-attach IDP views. Prefer in-memory live ids (snapshot-restored),
    # fall back to lobby_details files on disk.
    try:
        open_cancel_disabled = not constants.disabled_status
        t3_cancel_disabled = not constants.special_disabled_status
        seen = set()
        for k, v in list((constants.temp_json_dict or {}).items()):
            try:
                message = await bot.get_channel(int(v[1])).fetch_message(int(v[0]))
                await message.edit(view=IdpChannelTasksView(cancel_disabled=open_cancel_disabled))
                seen.add(str(k))
            except Exception as e:
                print(f"Got Exception {e} when dealing with live open lobby {k}")
        for k, v in list((constants.temp_json_dict2 or {}).items()):
            try:
                message = await bot.get_channel(int(v[1])).fetch_message(int(v[0]))
                await message.edit(view=IdpChannelTasksView(cancel_disabled=t3_cancel_disabled))
                seen.add(f"t3-{k}")
            except Exception as e:
                print(f"Got Exception {e} when dealing with live t3 lobby {k}")

        with open('lobby_details.json', 'r') as f:
            lobby_details_json = json.load(f)

        if lobby_details_json:
            for k,v in lobby_details_json.items():
                if str(k) in seen:
                    continue
                try:
                    message = await bot.get_channel(int(v[1])).fetch_message(int(v[0]))
                    await message.edit(view=IdpChannelTasksView(cancel_disabled=open_cancel_disabled))
                except Exception as e:
                    print(f"Got Exception {e} when dealing with lobby json file")

        with open('lobby_details2.json', 'r') as f:
            lobby_details_json2 = json.load(f)

        if lobby_details_json2:
            for k,v in lobby_details_json2.items():
                if f"t3-{k}" in seen:
                    continue
                try:
                    message = await bot.get_channel(int(v[1])).fetch_message(int(v[0]))
                    await message.edit(view=IdpChannelTasksView(cancel_disabled=t3_cancel_disabled))
                except Exception as e:
                    print(f"Got Exception {e} when dealing with lobby_details2 json file")
    except FileNotFoundError:
        print("lobby_details.json not found, CRITICAL PROBLEM BUT skipping...")

    # On startup: delete claim messages for lobbies that already started,
    # re-attach ClaimSlotView to everything still unclaimed. Claims have
    # no deadline: message existence + valid click = slot.
    try:
        await expire_stale_claim_messages(reason="startup")
    except Exception as e:
        print(f"[expire] startup sweep failed: {e}")
    try:
        cancelled_data = load_cancelled_slots()
        for key, entry in cancelled_data.items():
            if entry.get("claimed"):
                continue  # skip already claimed/expired slots

            is_t3 = entry["type"] == "t3"
            group = entry["group"]

            try:
                channel = bot.get_channel(entry["channel_id"])
                if channel:
                    message = await channel.fetch_message(entry["message_id"])
                    claim_view = ClaimSlotView(entry["lobby"], group, is_t3)
                    await message.edit(view=claim_view)
                    print(f"Re-attached claim view for {key}")
            except Exception as e:
                print(f"Could not re-attach claim view for {key}: {e}")
            # one-shot timer replaces the polling loop; reschedule survivors
            try:
                schedule_claim_expiry(key, int(entry["lobby"]), is_t3)
            except Exception as e:
                print(f"[expire] reschedule failed for {key}: {e}")
    except Exception as e:
        print(f"Error loading cancelled_slots.json on startup: {e}")

    for _loop in (start_auto, clear_lb_auto, auto_close_reg):
        if not _loop.is_running():
            _loop.start()
    for _loop in (t3rulesreminder, t3rulesreminder2):
        if not _loop.is_running():
            _loop.start()
    try:
        _spawn(_boot_thread_sweep())  # non-blocking: on_ready doesn't wait for it
        if not flow_thread_sweeper.is_running():
            flow_thread_sweeper.start()
        if not mod_panel_refresher.is_running():
            mod_panel_refresher.start()
    except Exception as e:
        print(f"[thread-sweep] could not start (non-fatal): {e}")
    # No polling loop: each claim message gets a one-shot expiry task at
    # M1 START (schedule_claim_expiry), rebuilt on startup above.
    # Get the process ID (PID) of the current program
    pid = os.getpid()
    process = psutil.Process(pid)

    # Get memory usage in bytes
    memory_info = process.memory_info()

    # Convert memory usage to MB
    memory_usage_mb = memory_info.rss / (1024 * 1024)  # rss is the Resident Set Size (physical memory)

    print(f"Memory Usage: {memory_usage_mb:.2f} MB")

    _setup_done = True
    print(f"Set bro.")

@bot.event
async def on_guild_join(guild):
    # Sync commands for the new guild
    await bot.tree.sync(guild=guild)
    print(f"Slash commands synced in new guild: {guild.name}")
    
def _google_key_path():
    """GOOGLE_APPLICATION_CREDENTIALS, or the key's usual place on the dev PC."""
    return os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "D:/Google Cloud JSON Key/clear-healer-415920-7abc2cda379e.json"

async def connect_to_google_sheets(json_keyfile_path, sheet_id,retry_interval=1):
    while True:
        try:
            credentials = Credentials.from_service_account_file(json_keyfile_path, scopes=['https://www.googleapis.com/auth/spreadsheets'])
            gc = gspread.authorize(credentials)
            # Cap Sheets calls: no timeout means one lost packet can stall the bot for minutes.
            # gspread 6.x: timeout lives on the client, not the worksheet. Set here so reconnects get it too.
            gc.set_timeout((5, 30))
            print("Successfully authenticated with Google Sheets.")

            service = build('sheets', 'v4', credentials=credentials)

            sheet = gc.open_by_key(sheet_id).sheet1
            print("Successfully opened Google Sheets document by ID:", sheet_id)

            return sheet, service

        except Exception as e:
            print("Error while connecting to Google Sheets:", e)
            print(f"Retrying in {retry_interval} seconds...")
            await asyncio.sleep(retry_interval)


async def start_registration(captcha_phrase: str):
    # Snapshot the state we are about to wipe. Without this, the only copy of a
    # day's registrations is the one written AFTER the clear below, which is
    # empty - that is how the previous run lost everything.
    try:
        state_snapshot.save_state_snapshot(suffix="prewipe")
    except Exception as e:
        print(f"[snapshot] pre-wipe snapshot failed (non-fatal): {e}")

    constants.registered_set = set()
    constants.registered_teams.clear()
    constants.lobby_teams = [{} for _ in range(
        int(constants.SLOTS_LIMIT) // int(constants.LOBBY_SIZE)
    )]
    constants.disabled_status = False
    constants.captcha_question_variables.clear()

    constants.special_registered_set = set()
    constants.special_registered_teams.clear()
    constants.special_lobby_teams = [{} for _ in range(
        int(constants.SPECIAL_SLOTS_LIMIT) // int(constants.SPECIAL_LOBBY_SIZE)
    )]
    constants.special_disabled_status = False
    _now_ts = time.time()
    constants.reg_started_at = {"open": _now_ts, "t3": _now_ts}
    constants.late_attempts = {"open": 0, "t3": 0}

    try:
        await bot.get_channel(constants.SPECIAL_REGISTRATION_CHANNEL_ID).purge(check=lambda m: m.id != constants.SPECIAL_REG_MESSAGE_ID, limit=100)
        special_message = await bot.get_channel(
            constants.SPECIAL_REGISTRATION_CHANNEL_ID
        ).fetch_message(constants.SPECIAL_REG_MESSAGE_ID)

        await special_message.edit(view=RegistrationView3())

    except Exception as e:
        print(f"Failed to reset special registration: {e}")

    try:
        await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).purge(
            check=lambda m: m.id != constants.REG_MESSAGE_ID,
            limit=100
        )

        print("Messages purged successfully.")

        with open('timestamps.csv', 'w', newline=''):
            pass

        message = await bot.get_channel(
            constants.REGISTRATION_CHANNEL_ID
        ).fetch_message(constants.REG_MESSAGE_ID)

        await message.edit(view=RegistrationView2())

        _atomic_write_json('lobby_details.json', {})

        _atomic_write_json('lobby_details2.json', {})

    except discord.HTTPException as e:
        print(f"An error occurred while purging messages: {e}")

    constants.captcha_question_variables.extend([
        captcha_phrase.lower().strip(),
        random.randint(10, 99),
        random.randint(10, 99),
        random.randint(10, 99),
        random.randint(10, 99),
    ])
    try:
        # allow_empty: this reset is intentional and the pre-wipe copy was taken
        # above. Without it the guard kept YESTERDAY's snapshot, so a crash or
        # reconnect before the first booking restored yesterday's teams,
        # captcha and closed state.
        state_snapshot.save_state_snapshot(allow_empty=True)
    except Exception as e:
        print(f"[snapshot] save failed (non-fatal): {e}")
    try:
        await refresh_reg_message(is_t3=False, show_slots=True, force=True)
        await refresh_reg_message(is_t3=True, show_slots=True, force=True)
    except Exception as e:
        print('[reg-slots] reset failed (non-fatal):', e)

@bot.hybrid_command(
    name="start",
    description="To Start REG, the captcha you pass in will be default for everyone."
)
@commands.has_any_role(*constants.roles_for_bot_access)
async def start(ctx, captcha_phrase: str):
    await ctx.defer()

    if not await is_clearlb_done_for_today():
        await ctx.send("Clearlb not done yet. Please run /clearlb first, then /start.")
        return

    await start_registration(captcha_phrase)

    await ctx.send(
        f"refresheeeeeeeeeeeeeeeeeeeed\n"
        f"Current captcha variables: "
        f"{constants.captcha_question_variables[0]}, "
        f"{constants.captcha_question_variables[1]} + "
        f"{constants.captcha_question_variables[2]}, "
        f"{constants.captcha_question_variables[3]} + "
        f"{constants.captcha_question_variables[4]}"
    )

@start.error
async def start_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="break", description="**Break Registration in between, Sensitive")
@app_commands.describe(target="which registration to break")
@app_commands.choices(target=[
    app_commands.Choice(name="main", value="main"),
    app_commands.Choice(name="special", value="special"),
])
@commands.has_any_role(*constants.roles_for_bot_access)
async def break_reg(ctx, target: str):
    await ctx.defer()

    is_t3 = target != "main"
    closed = await finalize_registration(is_t3, reason=f"/break by {ctx.author.name}")
    if closed:
        await ctx.send(f"Broke registration: {target}. Summary posted in <#{constants.UPDATES_CHANNEL_ID}>.")
    else:
        await ctx.send(f"{_reg_kind(is_t3)} registration was already closed, nothing changed.")

def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def build_close_summary(is_t3, reason):
    key = _reg_key(is_t3)
    counts = [len(_lobbies(is_t3)[i]) for i in range(_lobby_count(is_t3))]
    now = datetime.datetime.now(tz=constants.timezone)
    e = discord.Embed(title=f"{_reg_kind(is_t3)} registration closed", description=reason.capitalize(), color=0x229db7)
    started = (constants.reg_started_at or {}).get(key)
    if started:
        opened = datetime.datetime.fromtimestamp(started, tz=constants.timezone)
        e.add_field(name="Open for", value=f"{_fmt_duration(now.timestamp() - started)}\n{opened.strftime('%I:%M:%S %p')} to {now.strftime('%I:%M:%S %p')}", inline=True)
    e.add_field(name="Booked", value=f"{sum(counts)}/{_slots_limit(is_t3)}", inline=True)
    e.add_field(name="Late attempts", value=str((constants.late_attempts or {}).get(key, 0)), inline=True)
    size = _lobby_size(is_t3)
    e.add_field(name="Lobbies", value="```\n" + "\n".join(f"L{i} {_fill_bar(c, size)} {c:>2}/{size}" for i, c in enumerate(counts, 1)) + "\n```", inline=False)
    return e

async def finalize_registration(is_t3, reason="closed"):
    """Close Open or T3 registration once: disable the buttons, write and post
    the lobby files, post the slot lists, post a summary. Safe to call from
    several places at once (full lobbies, auto-close, /break, Mod Panel): only
    the first call does the work. Returns True if this call closed it."""
    lock = constants.special_registration_lock if is_t3 else constants.registration_lock
    async with lock:
        if not _reg_open(is_t3):
            return False
        if is_t3:
            constants.special_disabled_status = True
        else:
            constants.disabled_status = True
        state_snapshot.save_state_snapshot()

        updates = bot.get_channel(constants.UPDATES_CHANNEL_ID)
        reg_channel_id = constants.SPECIAL_REGISTRATION_CHANNEL_ID if is_t3 else constants.REGISTRATION_CHANNEL_ID
        reg_message_id = constants.SPECIAL_REG_MESSAGE_ID if is_t3 else constants.REG_MESSAGE_ID
        try:
            message = await bot.get_channel(reg_channel_id).fetch_message(reg_message_id)
            await message.edit(view=_view_for(is_t3))
        except Exception as e:
            print(f"[close] could not disable buttons (non-fatal): {e}")

        teams = _reg_teams(is_t3)
        lobbies = _lobbies(is_t3)
        if teams:
            csv_name = 'special_registered_teams.csv' if is_t3 else 'registered_teams.csv'
            try:
                await save_as_csv(teams, csv_name, save_all_flag=True)
                await updates.send(file=discord.File(csv_name))
            except Exception as e:
                print(f"[close] registered teams csv failed (non-fatal): {e}")

        async def _one_lobby(lobby_number, lobby_teams_dict):
            json_file_name = f"alt_lobby_{lobby_number}_teams.json" if is_t3 else f"lobby_{lobby_number}_teams.json"
            # Always rewrite the file, even empty: cancel / claim / add read it
            # after close, and yesterday's file must not leak into today.
            _atomic_write_json(json_file_name, lobby_teams_dict)
            if not lobby_teams_dict:
                return
            try:
                await updates.send(file=discord.File(json_file_name))
            except Exception as e:
                print(f"[close] sending {json_file_name} failed (non-fatal): {e}")
            try:
                idp_channel = _idp_channel(lobby_number, is_t3)
                await send_slots_list(list(lobby_teams_dict.keys()), lobby_number, idp_channel,
                                      use_alt_lobby=is_t3, edit_slots_list=True, cancel_disabled=False)
            except Exception as e:
                print(f"[close] slot list for lobby {lobby_number} failed (non-fatal): {e}")

        results = await asyncio.gather(*(_one_lobby(i, dict(d)) for i, d in enumerate(lobbies, 1)), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                print(f"[close] lobby step failed (non-fatal): {r}")

        if teams:
            try:
                await updates.send(
                    f"You can download the Google Sheets app to view the list of users and their registration timestamps of {datetime.datetime.now(tz=constants.timezone).strftime('%d %b')} from this CSV file (for transparency). If you cant find you name in these, you were later than all these \U0001f622.",
                    file=discord.File('timestamps.csv')
                )
            except Exception as e:
                print(f"[close] timestamps csv failed (non-fatal): {e}")

        try:
            _atomic_write_json('lobby_details2.json' if is_t3 else 'lobby_details.json',
                               constants.temp_json_dict2 if is_t3 else constants.temp_json_dict)
        except Exception as e:
            print(f"[close] lobby details write failed (non-fatal): {e}")
        try:
            await refresh_reg_message(is_t3=is_t3, show_slots=False, force=True)
        except Exception as e:
            print('[reg-slots] hide failed (non-fatal):', e)
        try:
            await updates.send(embed=build_close_summary(is_t3, reason))
        except Exception as e:
            print(f"[close] summary failed (non-fatal): {e}")
        state_snapshot.save_state_snapshot()
        return True


@break_reg.error
async def break_reg_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")


@bot.hybrid_command(name="show_limits", description="Show the current slots limit and lobby size.")
@commands.has_any_role(*constants.roles_for_bot_access)
async def show_limits(ctx):
    await ctx.send(f"Current slots limit: {constants.SLOTS_LIMIT}\nCurrent lobby size: {constants.LOBBY_SIZE}")

@bot.hybrid_command(name="delete_from_sheet",description="**SENSITIVE, this team's data can be lost forever from our end.")
@commands.has_any_role('++D','Admin','.',"Mahatma")
async def delete_from_sheet(ctx,member: discord.User):
    try:
        await ctx.defer()
        row = await delete_team_from_sheet(member.id,constants.GOOGLE_SHEET_ID,ctx=ctx)
        await ctx.send(f"Team data deleted successfully.\n{row}")
    except Exception as e:
        await ctx.send(f"Error occurred while deleting team data from Google Sheets \n{e}")

@delete_from_sheet.error
async def delete_from_sheet(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.tree.command(name="ban_team", description="Ban whole team for x hours and y days")
@app_commands.checks.has_permissions(view_audit_log=True, manage_roles=True)
async def ban_team(interaction: discord.Interaction, user: discord.User, hours: int = 0, days: int = 0):
    if hours == 0 and days == 0:
        await interaction.response.send_message("You must specify a valid duration.")
        return
    
    # Logic to ban the team goes here.
    # This is an example, assuming you have a way to get team members
    team_name = await validate_registration(user, check_cooldown = False,check_left_server = False)
    if team_name in constants.banned_team_list:
        await interaction.response.send_message("Bhai ye team already banned hai, if duration badhana h to splitz ko pakdo, aese command se krna thoda mushkil hai")
        return
    elif not team_name:
        await interaction.response.send_message("Couldn't find any team with this user.")
        return
    await interaction.response.defer()
    row = [team_name,int(time.time()),int((hours * 3600) + (days * 86400)),datetime.datetime.now(tz=constants.timezone).strftime("%Y-%m-%d %H:%M"),f"{days} days {hours} hours",str(user)]
    await asyncio.to_thread(constants.ban_sheet.append_row, row)
    await interaction.followup.send(f"Banned User: {user.mention}'s Team {team_name[5:] if team_name.lower().startswith('team ') else team_name} for {days} days and {hours} hours")
    # Print registration details for verification
    print(f"Banned {team_name} for {days} days and {hours} hours")

@ban_team.error
async def ban_team_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await _safe_reply(interaction, f"You don't have the required permissions to use this command: {missing_perms}", ephemeral=False)
    elif isinstance(error, ValueError):
        await _safe_reply(interaction, "You must specify a valid duration.", ephemeral=False)
    else:
        await _safe_reply(interaction, f"An error occurred: {error}", ephemeral=False)

@bot.tree.command(name="blacklist_user", description="Ban whole team for x hours and y days")
@app_commands.checks.has_permissions(view_audit_log=True, manage_roles=True)
async def blacklist_user(interaction: discord.Interaction, user: discord.User, hours: int = 0, days: int = 0, reason : str = ''):
    if hours == 0 and days == 0:
        await interaction.response.send_message("You must specify a valid duration.")
        return
    
    # Logic to ban the team goes here.
    # This is an example, assuming you have a way to get team members
    team_name = await validate_registration(user, check_cooldown = False,check_left_server = False)
    if team_name:
        await interaction.response.send_message("This team is currently present in sheet. \n(Blacklist prevents user from \"enroll\")")
        return
    elif not team_name:
        await interaction.response.defer()
        row = [str(user.id),int(time.time()),int((hours * 3600) + (days * 86400)),datetime.datetime.now(tz=constants.timezone).strftime("%Y-%m-%d %H:%M"),f"{days} days {hours} hours",reason]
        await asyncio.to_thread(constants.blacklist_sheet.append_row, row)
        await interaction.followup.send(f"Blacklisted User: {user.mention} for {days} days and {hours} hours")
        print(f"Blacklisted User: {user.mention}'s for {days} days and {hours} hours")

@blacklist_user.error
async def blacklist_user_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await _safe_reply(interaction, f"You don't have the required permissions to use this command: {missing_perms}", ephemeral=False)
    elif isinstance(error, ValueError):
        await _safe_reply(interaction, "You must specify a valid duration.", ephemeral=False)
    else:
        await _safe_reply(interaction, f"An error occurred: {error}", ephemeral=False)

@bot.hybrid_command(name="clear_amateur", description="**Clear Amateur lobby Channels and role**")
@commands.has_any_role(*constants.roles_for_purge_perm)
async def clear_amateur(ctx):
    await ctx.send("kr rha thoda wait krna ..")

    role_names = ["Amateur G1 IDP", "Amateur G2 IDP", "Amateur G3 IDP"]
    channel_names = [
        "amateur-g1-idp",
        "amateur-g2-idp",
        "amateur-g3-idp",
    ]

    try:
        # Remove roles from members
        for role_name in role_names:
            role = discord.utils.get(ctx.guild.roles, name=role_name)
            if role:
                for member in role.members:
                    await member.remove_roles(role)
                    print(f"Removed {role_name} role from {member}")

        # Purge messages from channels
        before = ctx.interaction.created_at if ctx.interaction else ctx.message.created_at
        for channel_name in channel_names:
            channel = discord.utils.get(ctx.guild.channels, name=channel_name)
            if channel:
                await channel.purge(limit=500, reason=f"amateur clearup by {ctx}", before=before)
                print(f"Purged messages from {channel_name}")

        await ctx.send("Amateur Lobby channels (last 24 hrs) and roles are cleared now.")

    except discord.Forbidden:
        await ctx.send("I do not have permission to manage roles or channels.")
    except discord.HTTPException as e:
        await ctx.send(f"An HTTP error occurred: {e}")
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

@clear_amateur.error
async def clear_amateur_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="rr", description="Fetch random users who reacted to a message")
@commands.has_any_role(*constants.roles_for_bot_access)
async def rr(ctx, num: int):
    
    try:

        if ctx.interaction:
            await ctx.send("Please use this as a normal command not / command.")
            return
            
        # Check if the command is used in reply to a message
        if ctx.message.reference and ctx.message.reference.message_id:
            message_id = ctx.message.reference.message_id
            message = await ctx.channel.fetch_message(message_id)
        else:
            await ctx.send("Please reply to the message you want to check reactions for.")
            return
        
        if not message.reactions:
            await ctx.send("No reactions found on the specified message.")
            return

        if len(message.reactions) > 1:
            await ctx.send("Multiple reactions found. Please specify which emote to use:")
            await ctx.send("\n".join([f"{i+1}. {reaction.emoji}" for i, reaction in enumerate(message.reactions)]))
            
            # Nested function to check for valid user input
            def check(m):
                return m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit() and 1 <= int(m.content) <= len(message.reactions)

            try:
                emote_choice = await bot.wait_for("message", check=check, timeout=60)
                chosen_reaction = message.reactions[int(emote_choice.content) - 1]
            except asyncio.TimeoutError:
                await ctx.send("You took too long to respond.")
                return
        else:
            chosen_reaction = message.reactions[0]

        users = [user async for user in chosen_reaction.users()]

        if num > len(users):
            await ctx.send(f"Requested number of users ({num}) exceeds the number of users who reacted ({len(users)}). Fetching all users instead.")
            num = len(users)

        random_users = random.sample(users, num)

        await ctx.send(f"Randomly selected users: " + ",".join([user.mention for user in random_users]))

    except discord.NotFound:
        await ctx.send("Message not found. Please ensure the message ID is correct.")
    except discord.HTTPException as e:
        await ctx.send(f"An HTTP error occurred: {e}")
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

@rr.error
async def rr(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")


@bot.hybrid_command(name="purge", description="Purge a specified number of messages from the channel.")
@commands.has_any_role(*constants.roles_for_purge_perm)
async def purge(ctx, number_of_messages: int):

    await ctx.defer(ephemeral=True)

    if number_of_messages <= 0:
        await ctx.send("Please specify a positive number of messages to purge.")
        return

    try:

        if ctx.interaction:
            await ctx.channel.purge(limit=number_of_messages,reason=f"{ctx} deleted {number_of_messages} messages.",before=ctx.interaction.created_at)
        else:
            await ctx.channel.purge(limit=number_of_messages,reason=f"{ctx} deleted {number_of_messages} messages.")

        purge_message = await ctx.send(f"Just deleted {number_of_messages} messages in this channel.")
        await asyncio.sleep(5)
        await purge_message.delete()
        print(f"Purged {number_of_messages} messages from {ctx.channel.name}.")
    except discord.HTTPException as e:
        print(f"An error occurred while purging messages: {e}")
        await ctx.send(f"An error occurred while purging messages: {e}")

@purge.error
async def purge_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(f"You don't have the required permissions to use this command.")
    elif isinstance(error, commands.MissingAnyRole): 
        await ctx.send(f"Sorry this command is pretty limited")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Please specify a valid number of messages to purge.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="say", description="""Make the bot say a specified message in a specified channel.""")
@commands.has_any_role(*constants.roles_for_bot_access)
async def say(ctx: commands.Context, channel: discord.TextChannel, *, message: str):
    try:
        await channel.send(message)
    except discord.HTTPException as e:
        await ctx.send(f"An error occurred while sending the message: {e}")

@say.error
async def say_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("The specified channel was not found.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="saythischannel", description="""Make the bot say a specified message in this channel.""")
@commands.has_any_role(*constants.roles_for_bot_access)
async def saythischannel(ctx: commands.Context, message: str):
    try:
        await ctx.channel.send(message)
    except discord.HTTPException as e:
        await ctx.send(f"An error occurred while sending the message: {e}")

@saythischannel.error
async def saythischannel_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("The specified channel was not found.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="faq", description="send faq view")
@commands.has_any_role(*constants.roles_for_bot_access)
async def faq(ctx: commands.Context, channel: discord.TextChannel, *, message: str):
    try:
        await channel.send(message, view=FaqView())
    except discord.HTTPException as e:
        await ctx.send(f"An error occurred while sending the message: {e}")

@faq.error
async def faq_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("The specified channel was not found.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="role_by_reply", description="Assign a specified role to all mentioned users in the replied message.")
@commands.has_any_role(*constants.roles_for_bot_access)
async def role_by_reply(ctx, role_id: int):
    
    if ctx.interaction:
            await ctx.send("Please use this as a normal command not / command.")
            return

    if not ctx.message.reference:
        await ctx.send("You need to reply to a message containing mentions to use this command.")
        return
    
    try:
        replied_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        mentioned_users = replied_message.mentions
        
        if not mentioned_users:
            await ctx.send("No users were mentioned in the replied message.")
            return
        
        for user in mentioned_users:
            try:

                role = discord.utils.get(ctx.guild.roles, id=role_id)
                if not role:
                    await ctx.send("Invalid role ID. Make sure the role exists and I have permission to manage it.")
                    return
                await user.add_roles(role)
            except discord.Forbidden:
                await ctx.send(f"I don't have permission to assign roles to {user.mention}.")
            except discord.HTTPException as e:
                await ctx.send(f"Failed to assign role to {user.mention}: {e}")
        
        await ctx.send(f"Assigned {role.name} to {len(mentioned_users)} users.")
        print(f"Assigned {role.name} to {[user.name for user in mentioned_users]} in {ctx.channel.name}.")
    except discord.NotFound:
        await ctx.send("Couldn't fetch the replied message. It might have been deleted.")
    except discord.HTTPException as e:
        await ctx.send(f"An error occurred while assigning roles: {e}")

@role_by_reply.error
async def role_by_reply_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Please specify a valid role.")
    else:
        await ctx.send(f"An error occurred: {error}")
        

@bot.hybrid_command(name="inrole", description="inrole users and team names")
@commands.has_any_role(*constants.roles_for_bot_access)
async def inrole(ctx: commands.Context, role: discord.Role):
    try:
        lines = [
            f"{member.mention} : {await validate_registration(member, check_cooldown=False, check_left_server=False)}"
            for member in role.members
        ]
        if not lines:
            await ctx.send(f"No members found in {role.name}.")
            return
        chunks, cur = [], ""
        for line in lines:
            if len(cur) + len(line) + 1 > 1900:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        chunks.append(cur)
        if len(chunks) > 5:
            await ctx.send(f"{role.name} has {len(lines)} members, too many to list here. Use `/inrole_csv {role.name}` for the full file.")
            return
        for chunk in chunks:
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        await ctx.send(f"An error occurred while sending the message: {e}")

@inrole.error
async def inrole_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("The specified channel was not found.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="inrole_csv", description="Export role members with team names and IGNs to a CSV file.")
@commands.has_any_role(*constants.roles_for_bot_access)
async def inrole_csv(ctx: commands.Context, role: discord.Role):
    try:
        await ctx.defer()
    except Exception:
        pass
    try:
        members = list(role.members)
        if not members:
            await ctx.send(f"No members found in {role.name}.")
            return

        # Single pass over the sheet cache: discord id -> (team name, igns).
        # Only the member's own dc id is exported; teammates' ids are skipped.
        id_lookup = {}
        try:
            rows = constants.cached_data or []
            for row in rows:
                try:
                    if not row or len(row) < 2:
                        continue
                    team = _report_clean_team_name(row[1])
                    if _report_is_garbage_team(team):
                        continue
                    if team.lower() in ("team_name", "team name", "team", "teamname"):
                        continue
                    igns = [str(x).strip() if x is not None else "" for x in row[3::2]]
                    igns = (igns + [""] * 5)[:5]
                    for dc_id in row[2::2]:
                        dc_id = str(dc_id).strip() if dc_id is not None else ""
                        if dc_id and dc_id not in id_lookup:
                            id_lookup[dc_id] = (team, igns)
                except Exception:
                    continue
        except Exception:
            pass

        out_rows = []
        for member in members:
            try:
                username = str(member.name)
            except Exception:
                username = str(member)
            hit = id_lookup.get(str(member.id))
            if hit:
                team, igns = hit
                out_rows.append([username, str(member.id), team] + list(igns))
            else:
                out_rows.append([username, str(member.id), "", "", "", "", "", ""])

        out_rows.sort(key=lambda r: (r[2].lower(), r[0].lower()))
        safe_role = re.sub(r"[^A-Za-z0-9_-]+", "_", role.name).strip("_") or "role"
        filename = f"inrole_{safe_role}_{datetime.datetime.now(tz=constants.timezone).strftime('%Y-%m-%d')}.csv"
        try:
            with open(filename, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["dc_username", "dc_id", "team_name",
                            "player1_ign", "player2_ign", "player3_ign",
                            "player4_ign", "player5_ign"])
                w.writerows(out_rows)
        except Exception as e:
            await ctx.send(f"Could not write CSV file: {e}")
            return

        with_note = sum(1 for r in out_rows if r[2])
        await ctx.send(content=f"{role.name}: {len(out_rows)} members, {with_note} matched to a team.",
                       file=discord.File(filename))
    except discord.HTTPException as e:
        await ctx.send(f"An error occurred while sending the message: {e}")
    except Exception as e:
        print(f"Error in inrole_csv command: {e}")
        try:
            await ctx.send(f"An error occurred: {e}")
        except Exception:
            pass

@inrole_csv.error
async def inrole_csv_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("The specified channel was not found.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="amateur_details_view", description="Ask Team Details from Amateur Teams.")
@commands.has_any_role(*constants.roles_for_bot_access)
async def amateur_details_view(ctx: commands.Context, channel: discord.TextChannel):
    try:
        message = "Click the button below to submit your team details."
        await channel.send(message, view=AmateurDetailsView())
    except discord.HTTPException as e:
        await ctx.send(f"An error occurred while sending the message: {e}")

@amateur_details_view.error
async def amateur_details_view_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("The specified channel was not found.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="modpanel", description="Post the Mod Panel (status, find team, add team, room ID, backup) here.")
@commands.has_any_role(*constants.roles_for_bot_access)
async def modpanel(ctx: commands.Context):
    try:
        # One panel at a time: remove the previous one so mods never click a stale copy.
        old = _load_mod_panel_ref()
        if old:
            try:
                old_msg = await bot.get_channel(old["channel_id"]).fetch_message(old["message_id"])
                await old_msg.delete()
            except Exception:
                pass
        msg = await ctx.channel.send(embed=mod_panel_embed(), view=ModPanelView())
        _save_mod_panel_ref(msg.channel.id, msg.id)
        if ctx.interaction:
            await ctx.send("Mod Panel posted. It stays here across restarts.", ephemeral=True)
    except discord.HTTPException as e:
        await ctx.send(f"Couldn't post the panel: {e}")

@modpanel.error
async def modpanel_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.MissingPermissions, commands.MissingAnyRole)):
        await ctx.send("You don't have the required permissions to use this command.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="add_team", description="Add team in T3 Slots List.")
@commands.has_any_role(*constants.roles_for_bot_access)
async def add_team(ctx: commands.Context, team_name: str,member: discord.Member,channel: discord.TextChannel):
    try:
        await add_team_slotlist(team_name,member,channel)
        await ctx.send(f"{team_name} added in {channel.mention} by {ctx.author.name}")
    except AddTeamError as e:
        await ctx.send(f"Can't add {team_name}: {e}")
    except discord.HTTPException as e:
        await ctx.send(f"Error aaya: {e}")

@add_team.error
async def add_team_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.MissingPermissions, commands.MissingAnyRole)):
        await ctx.send("You don't have the required permissions to use this command.")
    else:
        await ctx.send(f"An error occurred: {error}")

# ──────────────────────────────────────────────────────────────────
# Team registration report (OPEN lobby vs TIER3) from UPDATES channel
# ──────────────────────────────────────────────────────────────────
REPORT_MAX_DAYS = 180
REPORT_MIN_LOBBY_SIZE = 15

def _report_clean_team_name(name):
    if name is None:
        return ""
    cleaned = re.sub(r"\s+", " ", str(name)).strip()
    return cleaned

def _report_is_garbage_team(name):
    if not name:
        return True
    up = name.strip().upper()
    if not up:
        return True
    if up.startswith("CANCELLED"):
        return True
    if up in ("__", "EMPTY", "RESERVED", "-"):
        return True
    return False

def _report_lobby_file_info(filename):
    fname = str(filename).strip().lower()
    m = re.match(r"^alt_lobby_(\d+)_teams\.json$", fname)
    if m:
        return ("TIER3", int(m.group(1)))
    m = re.match(r"^lobby_(\d+)_teams\.json$", fname)
    if m:
        return ("OPEN", int(m.group(1)))
    return None

def _report_group_for(lobby_number, reg_type):
    lobby_map = constants.GROUP_LOBBY_MAP2 if reg_type == "TIER3" else constants.GROUP_LOBBY_MAP
    try:
        for group, lobbies in lobby_map.items():
            if int(lobby_number) in list(lobbies):
                return str(group)
    except Exception:
        pass
    return ""

def _report_parse_lobby_json(raw_bytes, include_cancelled=False):
    try:
        text = raw_bytes.decode("utf-8", errors="ignore")
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    entries = []
    for k, v in data.items():
        team = _report_clean_team_name(k)
        booker = "" if v is None else str(v).strip()
        is_cancelled_slot = team.strip().upper().startswith("CANCELLED") or booker.lower() == "cancelled"
        if is_cancelled_slot:
            if not include_cancelled:
                continue
            if not team or team.strip().upper().startswith("CANCELLED"):
                team = team if team else "CANCELLED"
            booker = ""
            entries.append((team, booker))
            continue
        if _report_is_garbage_team(team):
            continue
        if booker and not booker.isdigit():
            booker = ""
        entries.append((team, booker))
    return entries

def _report_parse_timestamps(raw_bytes):
    try:
        text = raw_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return []
    if not text.strip():
        return []
    entries = []
    try:
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 4:
                continue
            username = str(row[0]).strip()
            ts_raw = str(row[1]).strip()
            lobby_raw = str(row[2]).strip()
            status = str(row[3]).strip().upper()
            if not username or not ts_raw:
                continue
            if status not in ("BOOKED", "LATE"):
                continue
            m = re.search(r"(\d+)", lobby_raw)
            lobby_no = int(m.group(1)) if m else None
            time_only = ""
            mt = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", ts_raw)
            if mt:
                time_only = mt.group(1)
                if len(time_only) == 5:
                    time_only += ":00"
            entries.append({
                "username": username,
                "username_lower": username.lower(),
                "timestamp_raw": ts_raw,
                "time_only": time_only,
                "lobby": lobby_no,
                "status": status,
            })
    except Exception:
        pass
    return entries

@bot.hybrid_command(name="report", description="Report of registered teams for the last N days. Max 6 months (180 days).")
@app_commands.describe(days="Last N days to include, ending today (IST). 1-180.", ignore_cancelled="When true, skip CANCELLED slots and lobbies with fewer than 15 teams.")
@commands.has_any_role(*constants.roles_for_purge_perm)
async def report(ctx: commands.Context, days: int, ignore_cancelled: bool = True):
    try:
        await ctx.defer()
    except Exception:
        pass
    try:
        try:
            days = int(days)
        except (TypeError, ValueError):
            await ctx.send("Days must be a number, e.g. `/report days:7`.")
            return
        if not 1 <= days <= REPORT_MAX_DAYS:
            await ctx.send(f"Days must be 1-{REPORT_MAX_DAYS} (max 6 months).")
            return
        today_ist = datetime.datetime.now(tz=constants.timezone).date()
        start_obj = today_ist - datetime.timedelta(days=days - 1)
        try:
            after_ist = constants.timezone.localize(datetime.datetime.combine(start_obj, datetime.time.min))
        except Exception:
            after_ist = datetime.datetime.combine(start_obj, datetime.time.min).replace(tzinfo=constants.timezone)
        after_utc = after_ist.astimezone(datetime.timezone.utc)

        channel = bot.get_channel(constants.UPDATES_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(constants.UPDATES_CHANNEL_ID)
            except Exception:
                channel = None
        if channel is None:
            await ctx.send("Could not access the updates channel.")
            return

        progress_msg = None
        try:
            progress_msg = await ctx.send(f"Scanning <#{constants.UPDATES_CHANNEL_ID}> `{start_obj.isoformat()}` to `{today_ist.isoformat()}` ... :eyes:")
        except Exception:
            progress_msg = None

        prog_state = {"scanned": 0}
        prog_stop = asyncio.Event()

        async def _report_progress_tick():
            frames = ["...", "....", ".....", "......"]
            i = 0
            while not prog_stop.is_set():
                if progress_msg is not None:
                    try:
                        await progress_msg.edit(content=f"Scanning <#{constants.UPDATES_CHANNEL_ID}> `{start_obj.isoformat()}` to `{today_ist.isoformat()}`{frames[i % len(frames)]} :eyes: (checked {prog_state['scanned']} msgs)")
                    except Exception:
                        pass
                i += 1
                try:
                    await asyncio.wait_for(prog_stop.wait(), timeout=10)
                except asyncio.TimeoutError:
                    continue

        async def _report_progress_stop(final_text=None):
            try:
                prog_stop.set()
            except Exception:
                pass
            try:
                await progress_task
            except Exception:
                pass
            if progress_msg is not None and final_text is not None:
                try:
                    await progress_msg.edit(content=final_text)
                except Exception:
                    pass

        progress_task = asyncio.create_task(_report_progress_tick())

        records = []
        timestamp_pools = []
        scanned = 0
        try:
            async for msg in channel.history(after=after_utc, oldest_first=True, limit=None):
                scanned += 1
                prog_state["scanned"] = scanned
                if not msg.attachments:
                    continue
                try:
                    msg_date_ist = msg.created_at.astimezone(constants.timezone).date()
                except Exception:
                    continue
                if msg_date_ist < start_obj or msg_date_ist > today_ist:
                    continue
                for att in msg.attachments:
                    info = _report_lobby_file_info(att.filename)
                    try:
                        if info is not None:
                            reg_type, lobby_no = info
                            raw = await att.read()
                            entries = _report_parse_lobby_json(raw, include_cancelled=not ignore_cancelled)
                            group = _report_group_for(lobby_no, reg_type)
                            for idx, (team, booker) in enumerate(entries, start=1):
                                records.append({
                                    "date": msg_date_ist.isoformat(),
                                    "type": reg_type,
                                    "team_name": team,
                                    "team_lower": team.lower(),
                                    "group": group,
                                    "lobby": lobby_no,
                                    "slot_no": idx,
                                    "booked_by_id": booker,
                                })
                        elif att.filename.strip().lower() == "timestamps.csv":
                            raw = await att.read()
                            ts_entries = _report_parse_timestamps(raw)
                            if ts_entries:
                                timestamp_pools.append({"at": msg.created_at, "date": msg_date_ist, "entries": ts_entries})
                    except Exception as e:
                        print(f"Report: skipped attachment {att.filename}: {e}")
                        continue
        except Exception as e:
            await _report_progress_stop(f"Scan failed :eyes: {e}")
            await ctx.send(f"Failed while scanning history: {e}")
            return

        if not records:
            await _report_progress_stop(f"Scan done, no data :eyes: (checked {scanned} msgs)")
            await ctx.send(f"No registration data found in <#{constants.UPDATES_CHANNEL_ID}> from `{start_obj.isoformat()}` to `{today_ist.isoformat()}` (scanned {scanned} messages).")
            return

        guild = bot.get_guild(constants.GUILD_ID)

        seen = set()
        deduped = []
        for rec in records:
            key = (rec["date"], rec["type"], rec["team_lower"], int(rec["lobby"]))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(rec)

        dropped_groups = 0
        dropped_rows = 0
        if ignore_cancelled:
            from collections import Counter
            lobby_counts = Counter((r["date"], r["type"], int(r["lobby"])) for r in deduped)
            kept = []
            for r in deduped:
                if lobby_counts[(r["date"], r["type"], int(r["lobby"]))] < REPORT_MIN_LOBBY_SIZE:
                    dropped_rows += 1
                    continue
                kept.append(r)
            dropped_groups = sum(1 for k, c in lobby_counts.items() if c < REPORT_MIN_LOBBY_SIZE)
            deduped = kept
            if not deduped:
                await _report_progress_stop("Scan done, all rows filtered :eyes:")
                await ctx.send(f"No rows left after filters (dropped {dropped_rows} rows in {dropped_groups} lobbies with fewer than {REPORT_MIN_LOBBY_SIZE} teams). Try `/report` with `ignore_cancelled:False`.")
                return

        open_rows = []
        t3_rows = []
        for rec in deduped:
            booking_time = ""
            try:
                booker_id = str(rec.get("booked_by_id", "")).strip()
                member_name = ""
                if booker_id.isdigit() and guild is not None:
                    m = guild.get_member(int(booker_id))
                    if m is not None:
                        try:
                            member_name = str(m.name).strip().lower()
                        except Exception:
                            member_name = ""
                        if not member_name:
                            try:
                                member_name = str(m.display_name).strip().lower()
                            except Exception:
                                member_name = ""
                for pool in timestamp_pools:
                    if pool["date"].isoformat() != rec["date"]:
                        continue
                    for e in pool["entries"]:
                        if e.get("status") != "BOOKED":
                            continue
                        if e.get("lobby") is not None and int(e["lobby"]) != int(rec["lobby"]):
                            continue
                        if member_name and e.get("username_lower") == member_name and e.get("time_only"):
                            booking_time = e["time_only"]
                            break
                    if booking_time:
                        break
            except Exception:
                booking_time = ""

            row = [
                rec["date"],
                rec["team_name"],
                rec["group"],
                int(rec["lobby"]),
                int(rec["slot_no"]),
                rec["booked_by_id"],
                booking_time,
            ]
            if rec["type"] == "OPEN":
                open_rows.append(row)
            else:
                t3_rows.append(row)

        open_rows.sort(key=lambda r: (r[0], int(r[3]), int(r[4])))
        t3_rows.sort(key=lambda r: (r[0], int(r[3]), int(r[4])))

        header = ["date", "team_name", "group", "lobby", "slot_no", "captain_discord_id",
                  "registration_time"]
        tag = f"{start_obj.isoformat()}_to_{today_ist.isoformat()}"
        open_file = f"report_open_{tag}.csv"
        t3_file = f"report_t3_{tag}.csv"
        try:
            with open(open_file, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(open_rows)
            with open(t3_file, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(t3_rows)
        except Exception as e:
            await _report_progress_stop("Report failed :eyes:")
            await ctx.send(f"Could not write report files: {e}")
            return

        open_unique = len({str(r[1]).lower() for r in open_rows})
        t3_unique = len({str(r[1]).lower() for r in t3_rows})
        days_hit = sorted({r["date"] for r in deduped})
        filt_note = f"Ignored CANCELLED + dropped {dropped_rows} rows in {dropped_groups} lobbies < {REPORT_MIN_LOBBY_SIZE} teams." if ignore_cancelled else "Filters off: CANCELLED kept, small lobbies kept."
        summary = (f"Report last {days} days: `{start_obj.isoformat()}` to `{today_ist.isoformat()}`\n"
                   f"OPEN sheet: {len(open_rows)} rows, {open_unique} unique | "
                   f"T3 sheet: {len(t3_rows)} rows, {t3_unique} unique | Days: {len(days_hit)}\n"
                   f"{filt_note}\n"
                   f"Source: <#{constants.UPDATES_CHANNEL_ID}> lobby JSONs "
                   f"(OPEN = Groups A-B, Lobbies 1-6; T3 = Groups A-C, Lobbies 1-3).")
        try:
            to_send = []
            if open_rows:
                to_send.append(discord.File(open_file))
            if t3_rows:
                to_send.append(discord.File(t3_file))
            if not to_send:
                await _report_progress_stop("Report done, no rows :eyes:")
                await ctx.send(content=summary + "\nNo rows in either sheet.")
                return
            await _report_progress_stop(f"Report done :eyes: (checked {scanned} msgs)")
            if len(to_send) == 1:
                await ctx.send(content=summary, file=to_send[0])
            else:
                await ctx.send(content=summary, files=to_send)
        except Exception as e:
            await _report_progress_stop("Report failed :eyes:")
            await ctx.send(f"Report ready (OPEN {len(open_rows)}, T3 {len(t3_rows)}) but file send failed: {e}")
    except Exception as e:
        print(f"Error in report command: {e}")
        try:
            prog_stop.set()
        except Exception:
            pass
        try:
            await ctx.send(f"Report failed: {e}")
        except Exception:
            pass

@report.error
async def report_error(ctx: commands.Context, error: commands.CommandError):
    try:
        if isinstance(error, commands.MissingAnyRole) or isinstance(error, commands.MissingRole):
            await ctx.send("You don't have the required permissions to use this command.")
        else:
            await ctx.send(f"An error occurred: {error}")
    except Exception:
        pass

@bot.tree.command(name="upload_results", description="Share all t3 results.")
@app_commands.checks.has_any_role(*constants.roles_for_purge_perm)
async def upload_results(interaction: discord.Interaction, results: str):
    try:
        await interaction.response.send_message("okay workin", ephemeral=True, delete_after=3)

        teams = results.split("<>")

        lobbies = _lobby_count(False)
        if len(teams) not in (lobbies, 16) or any(t.strip() != "__" for t in teams[lobbies:]):
            await interaction.channel.send(f"Error: give {lobbies} teams separated by '<>' (one per Open lobby, __ to skip). Tip: Promote winner in each lobby desk does this one lobby at a time.")
            return
        teams = teams[:lobbies]
        
        for i, team in enumerate(teams, start=1):

            # Skip empty teams
            if team == "__":
                continue

            try:
                await share_lobby_results(
                    lobby_number=i,
                    team_name=team
                )
                
                await interaction.channel.send(f"Lobby {i} results updated: {team}")

            except Exception as e:
                print(f"Exception in lobby {i} results: {e}")
                await interaction.channel.send(f"Could not update Lobby {i} results.")
        
    except Exception as e:
        await interaction.channel.send(f"An error occurred: {e}")


@upload_results.error
async def upload_results_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await interaction.response.send_message(
            f"You don't have the required permissions to use this command: {missing_perms}"
        )

# Pin to IST explicitly. Reading the system timezone made every scheduled task
# drift by 5:30 whenever the host/container ran in UTC, while the rest of the
# bot used constants.timezone (hardcoded Asia/Kolkata).
# NOTE: use zoneinfo, not pytz, for tzinfo=. A pytz zone keeps the historical
# LMT offset (+05:53) when passed straight to datetime.time(), which fires
# these loops 23 min early. zoneinfo.tzinfo= resolves the offset correctly.
from zoneinfo import ZoneInfo as _ZoneInfo
local_tz = _ZoneInfo("Asia/Kolkata")

# Fail loudly at import instead of silently running on UTC. Every scheduled
# loop below derives from local_tz, so if this is wrong the whole bot fires
# 5:30 off and can wipe lobbies at the wrong time. Checked here in code so it
# never depends on the host/container TZ or a TZ env var being set.
def _assert_ist_schedule():
    probe = datetime.datetime.combine(
        datetime.date(2026, 1, 15), datetime.time(12, 0, tzinfo=local_tz), tzinfo=local_tz
    )
    off = probe.utcoffset()
    if off != datetime.timedelta(hours=5, minutes=30):
        raise RuntimeError(
            f"Scheduled tasks are not pinned to IST: 12:00 resolved to offset "
            f"{off}, expected +05:30. Refusing to start so lobbies are not "
            f"wiped at the wrong time."
        )
    # Also make sure no loop was left using the system timezone.
    if _ZoneInfo("Asia/Kolkata").utcoffset(probe.replace(tzinfo=None)) != off:
        raise RuntimeError("Asia/Kolkata tzinfo inconsistent; refusing to start.")

_assert_ist_schedule()
x = datetime.time(hour=12, minute=0, tzinfo=local_tz)
@tasks.loop(time=x)
async def start_auto():
    today = datetime.datetime.now(local_tz).weekday()

    if today in constants.days_to_run:
        if not await is_clearlb_done_for_today():
            print('REG START auto: clearlb not done, clearing now')
            try:
                await clear_lobbies()
            except Exception as e:
                print(f'Auto clear before start failed: {e}')
                try:
                    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
                        f'Auto-clear before reg failed: {e}. Skipping auto-start.'
                    )
                except Exception:
                    pass
                return
            for _ch_id in (constants.REGISTRATION_CHANNEL_ID, constants.SPECIAL_REGISTRATION_CHANNEL_ID):
                try:
                    await bot.get_channel(_ch_id).send(
                        'Lobbies cleared. Reg starts in 5 minutes.'
                    )
                except Exception as e:
                    print(f'Could not send reg-in-3-min notice to {_ch_id}: {e}')
            await asyncio.sleep(300)
            if not await is_clearlb_done_for_today():
                print('Auto-clear incomplete, aborting auto-start')
                try:
                    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
                        'Auto-clear did not fully clear IDP channels. Skipping auto-start. Please run /clearlb then /start.'
                    )
                except Exception:
                    pass
                return
        print("REG STARTED!")

        captcha_phrase = ''.join(
            random.choice(ascii_lowercase)
            for _ in range(random.randint(5, 6))
        )

        await start_registration(captcha_phrase)

        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
            f"*REG STARTED!*\n"
            f"Current captcha variables: "
            f"{constants.captcha_question_variables[0]}, "
            f"{constants.captcha_question_variables[1]} + "
            f"{constants.captcha_question_variables[2]}, "
            f"{constants.captcha_question_variables[3]} + "
            f"{constants.captcha_question_variables[4]}"
        )

async def _channel_has_recent_msg(channel_name, hours=24):
    """True if channel has any message in last N hours. False on missing/error."""
    try:
        guild = bot.get_guild(constants.GUILD_ID)
        if guild is None:
            return False
        ch = discord.utils.get(guild.channels, name=channel_name)
        if ch is None:
            return False
        after = discord.utils.utcnow() - datetime.timedelta(hours=hours)
        async for _ in ch.history(limit=1, after=after):
            return True
        return False
    except Exception as e:
        print(f"[clearlb-check] {channel_name}: {e}")
        return False

async def is_clearlb_done_for_today():
    """Derived: cleared if probe IDP channels have no recent msgs (open + T3)."""
    try:
        open_has = await _channel_has_recent_msg("group-1-idp")
        t3_has = await _channel_has_recent_msg("t3-idp-1")
        return not (open_has or t3_has)
    except Exception as e:
        print(f"[clearlb-check] failed: {e}")
        return False

async def clear_lobbies(purge_all=False, before_time=None, progress=None):
    """Remove every IDP role and purge every IDP channel (last 24 h unless
    purge_all). Roles and channels are processed in parallel with a small
    limit; progress(text) is awaited with a short status when given."""
    guild = bot.get_guild(constants.GUILD_ID)
    roles = []
    channels = []
    for is_t3 in (False, True):
        for n in range(1, _lobby_count(is_t3) + 1):
            role = discord.utils.get(guild.roles, name=f"T3 G{n} IDP" if is_t3 else f"Group {n} IDP")
            if role:
                roles.append(role)
            channel = _idp_channel(n, is_t3)
            if channel:
                channels.append(channel)

    pairs = [(m, r) for r in roles for m in list(r.members)]
    done = {"roles": 0, "channels": 0}
    errors = []
    sem = asyncio.Semaphore(5)
    last_report = [0.0]

    async def report(force=False):
        if progress is None:
            return
        now = time.monotonic()
        if not force and now - last_report[0] < 2:
            return
        last_report[0] = now
        try:
            await progress(f"Roles removed: {done['roles']}/{len(pairs)}  |  Channels cleared: {done['channels']}/{len(channels)}")
        except Exception:
            pass

    async def drop(member, role):
        async with sem:
            try:
                await member.remove_roles(role, reason="daily lobby clear")
            except Exception as e:
                errors.append(f"{member}: {e}")
            done["roles"] += 1
            await report()

    async def purge(channel):
        async with sem:
            try:
                if purge_all:
                    await channel.purge()
                elif before_time:
                    await channel.purge(after=(datetime.datetime.now() - datetime.timedelta(hours=24)), before=before_time)
                else:
                    await channel.purge(after=(datetime.datetime.now() - datetime.timedelta(hours=24)))
            except Exception as e:
                errors.append(f"#{channel.name}: {e}")
            done["channels"] += 1
            await report()

    await report(force=True)
    await asyncio.gather(*(drop(m, r) for m, r in pairs))
    await asyncio.gather(*(purge(c) for c in channels))
    await report(force=True)
    if errors:
        print(f"[clearlb] {len(errors)} problem(s): {errors[:5]}")
    return len(pairs), len(channels), errors

@bot.hybrid_command(name="clearlb", description="**Clear lobby Channels and role")
@commands.has_any_role(*constants.roles_for_bot_access)
async def clear_lb(ctx):
    status = await ctx.send("Clearing lobbies...")

    async def progress(text):
        await status.edit(content=f"Clearing lobbies... {text}")

    try:
        roles, channels, errors = await clear_lobbies(before_time=ctx.message.created_at, progress=progress)
        tail = f" {len(errors)} item(s) failed, see logs." if errors else ""
        await status.edit(content=f"\u2705 Lobby channels (last 24 hrs) and roles are cleared now. {roles} roles removed, {channels} channels cleared.{tail}")
    except discord.Forbidden:
        await ctx.send("I do not have permission to manage roles or channels.")
    except discord.HTTPException as e:
        await ctx.send(f"An HTTP error occurred: {e}")
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

@bot.hybrid_command(name='clearthreads', description='clear enroll team inactive threads > 1 HR')
@commands.has_any_role(*constants.roles_for_purge_perm)
async def clear_threads(ctx):
    await ctx.defer()
    guild = bot.get_guild(constants.GUILD_ID)
    channel = guild.get_channel(constants.ENROLLMENT_CHANNEL_ID) if guild else None
    if channel is None:
        await ctx.send('Enrollment channel not found.')
        return
    cutoff = discord.utils.utcnow() - datetime.timedelta(hours=1)
    seen = set()
    to_check = []
    for t in list(getattr(channel, 'threads', []) or []):
        if t.id not in seen:
            seen.add(t.id)
            to_check.append(t)
    try:
        async for t in channel.archived_threads(limit=100, private=False):
            if t.id not in seen:
                seen.add(t.id)
                to_check.append(t)
    except Exception as e:
        print('[clearthreads] archived public fetch failed:', e)
    try:
        async for t in channel.archived_threads(limit=100, private=True):
            if t.id not in seen:
                seen.add(t.id)
                to_check.append(t)
    except Exception as e:
        print('[clearthreads] archived private fetch failed:', e)
    deleted = 0
    kept = 0
    for thread in to_check:
        try:
            last = None
            async for msg in thread.history(limit=1):
                last = msg.created_at
                break
            if last is None:
                last = getattr(thread, 'created_at', None)
            if last is None:
                kept += 1
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            if last < cutoff:
                await thread.delete()
                deleted += 1
            else:
                kept += 1
        except Exception as e:
            print('[clearthreads] delete failed:', e)
    await ctx.send('Cleared ' + str(deleted) + ' inactive threads (>1hr). ' + str(kept) + ' kept.')


y = datetime.time(hour=11, minute=45, tzinfo=local_tz)
@tasks.loop(time=y)
async def clear_lb_auto():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        try:
            if today == 1:
                await clear_lobbies(purge_all=True)
            else:
                await clear_lobbies()

            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"*CLEARED LOBBIES!*")
        
        except Exception as e:
            print(f"Error in clear_lb_auto: {e}")

@tasks.loop(minutes=1)
async def auto_close_reg():
    try:
        now = datetime.datetime.now(tz=constants.timezone)
        if now.weekday() not in constants.days_to_run:
            return
        for is_t3 in (False, True):
            try:
                sched = constants.match_schedule_t3 if is_t3 else constants.match_schedule
                earliest = None
                for lobby_number in sched:
                    start = get_lobby_m1_start(lobby_number, is_t3)
                    if start is None:
                        continue
                    if earliest is None or start < earliest:
                        earliest = start
                if earliest is None:
                    continue
                if now >= earliest - datetime.timedelta(minutes=30) and _reg_open(is_t3):
                    print(f'[auto-close] closing {_reg_kind(is_t3)} reg 30 min before M1')
                    await finalize_registration(is_t3, reason="auto-close, 30 min before match 1")
            except Exception as e:
                print('[auto-close] failed (non-fatal):', e)
    except Exception as e:
        print('[auto-close] loop failed (non-fatal):', e)


POV_STEPS = """Hello Teams,

Please follow these steps to record your Point of View (POV) while playing BGMI:
1. Before opening the BGMI app show the list of background running apps on your device
2. Go to the PlayStore (for Android users) or Appstore (for iOS users) and open the BGMI app.
3. Join the Lobby using the provided details.
4. Before starting the match, ensure that both your in-game audio and your own voice (microphone) are being recorded.
5. Play the match.
6. After each match, make sure to show the list of background running apps and IMEI on your device.
7. You need to repeat the above steps for every match you play."""

async def _post_to_all_idp(text):
    """Post the same text in every Open and T3 IDP channel (T3 used to be skipped)."""
    for is_t3 in (False, True):
        for n in range(1, _lobby_count(is_t3) + 1):
            channel = _idp_channel(n, is_t3)
            if channel is None:
                continue
            try:
                await channel.send(text)
            except Exception as e:
                print(f"[reminder] {channel.name}: {e}")

rulesremindertime = datetime.time(hour=13, minute=00, tzinfo=local_tz)
@tasks.loop(time=rulesremindertime)
async def t3rulesreminder():
    if datetime.datetime.now(local_tz).weekday() in constants.days_to_run:
        await _post_to_all_idp("Reminder to read rules written in slots list and rules channel.")

rulesremindertime2 = datetime.time(hour=13, minute=50, tzinfo=local_tz)
@tasks.loop(time=rulesremindertime2)
async def t3rulesreminder2():
    if datetime.datetime.now(local_tz).weekday() in constants.days_to_run:
        await _post_to_all_idp(POV_STEPS)

_RESERVED_TEAM_NAMES = {"cooldown", "banned", "left_server", "cancel"}

def _ign_problem(ign):
    if not ign:
        return "it can't be empty"
    if "\n" in ign:
        return "it must be a single line"
    if "<@" in ign or "@" in ign:
        return "it can't contain mentions or @"
    return None

async def _ask_ign(user, thread, number, total_steps, step, embed=None):
    prompt = f"`{step}/{total_steps}`  Player {number}'s IGN?"
    while True:
        ign = await get_user_response_in_thread(user, thread, prompt, embed=embed)
        problem = _ign_problem(ign)
        if not problem:
            return ign
        embed = None
        prompt = f"That IGN won't work because {problem}. Please enter Player {number}'s IGN again:"

async def enrollTeam(user, interaction):
    token = await _claim_process(user.id, "enrollment")
    thread = None
    try:
        try:
            thread = await create_private_thread(user, "enroll")
        except Exception as e:
            await _safe_reply(interaction, f"Couldn't open your enrollment thread right now ({e}). Please try again in a minute.")
            return
        _set_process_thread(user.id, token, thread.id)
        await _safe_reply(interaction, f"Your enrollment thread is ready: {thread.mention}")
        await thread.send(
            f"{user.mention} Let's enroll your team. Answer each step here.\n"
            "Made a mistake? Tap Cancel or type `cancel` anytime and start again.",
            view=FlowCancelView(user.id))

        total_steps = 7
        # Step 1: a unique, valid team name (re-asked until it passes)
        prompt = f"`1/{total_steps}`  What's your team name?"
        while True:
            team_name = await get_user_response_in_thread(user, thread, prompt)
            prompt = "Please enter a different team name:"
            if not team_name:
                continue
            if team_name.lower() in _RESERVED_TEAM_NAMES:
                await thread.send(f"{user.mention} This team name is not allowed. Please choose a different team name.")
                continue
            if "<@" in team_name or "@" in team_name or "\n" in team_name:
                await thread.send(f"{user.mention} Team names cannot contain mentions or multiple lines.")
                continue
            if len(team_name) > 25:
                await thread.send(f"{user.mention} Too long team name ({len(team_name)}/25 characters), Please choose a shorter one.")
                continue
            if not await is_team_name_unique(team_name):
                await thread.send(f"{user.mention} This team name already exists.\nYou can modify the team name slightly for it to pass.\nEx: Team Chambal Ke Daku can be written any way like ChambalKeDaku, Chambal Daket, Chambal ESP, Team Chambal, Chambal Squad.")
                continue
            break

        # Steps 2-5: four IGNs, each re-asked on a bad value instead of
        # throwing the whole enrollment away
        player_igns = []
        tag_embed = discord.Embed(description="All players' in-game names (IGN) must include a team acronym as prefix/suffix (SAME NAME TAG). Players without this will not be allowed in the lobby.", color=0x229db7)
        for i in range(1, 5):
            player_igns.append(await _ask_ign(user, thread, i, total_steps, i + 1, embed=tag_embed if i == 1 else None))

        # Step 6: fifth player, one tap
        fifth = await ask_yes_no_question_in_thread(user, thread, f"`6/{total_steps}`  Do you have a fifth player?")
        if fifth is None:
            raise asyncio.TimeoutError()
        if fifth == 'yes':
            player_igns.append(await _ask_ign(user, thread, 5, total_steps, 6))

        # Step 7: mentions + final validation
        await send_registration_details(user, team_name, player_igns, thread)
        validate_result = await validate_enrollment(user, team_name, player_igns, thread)
        _schedule_thread_delete(thread, 60 if validate_result else 0)

    except FlowCancelled:
        if thread is not None:
            try:
                await thread.send("Enrollment cancelled. Pick Enroll from the menu whenever you're ready.")
            except Exception:
                pass
        _schedule_thread_delete(thread, 5)

    except EnrollmentError as ee:
        # Keep the thread around so the user can read why, but release the
        # lock now so they can retry right away.
        try:
            if ee.text:
                await thread.send(ee.text)
            await thread.send(f"This thread will be deleted in {round(int(ee.timeout) / 60, 1)} minutes. You can start a new enrollment from the menu right away.")
        except Exception:
            pass
        _schedule_thread_delete(thread, int(ee.timeout))

    except asyncio.TimeoutError:
        try:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Enrollment timed out. \nPlease try again later.")
        except Exception:
            pass
        _schedule_thread_delete(thread, 0)

    except ValueError as ve:
        try:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An error occurred during enrollment: {ve}")
        except Exception:
            pass
        _schedule_thread_delete(thread, 0)

    except Exception as e:
        print(f"[enroll] unexpected error for {user}: {e}")
        try:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An unexpected error occurred during enrollment: {e}")
        except Exception:
            pass
        _schedule_thread_delete(thread, 0)

    finally:
        await _release_process(user.id, token)
        _reset_enroll_menu(getattr(interaction, "message", None))

async def create_private_thread(user, name_suffix):
    # Create the private thread with the provided suffix
    thread_name = f"{user.name}-{name_suffix}"
    private_thread = await bot.get_guild(constants.GUILD_ID).get_channel(constants.ENROLLMENT_CHANNEL_ID).create_thread(name=thread_name,invitable=True)

    # Add the user to the private thread
    await private_thread.add_user(user)

    # Return the private thread
    return private_thread

async def updateTeam(user, existing_team_message, interaction):
    token = await _claim_process(user.id, "team update")
    thread = None
    try:
        try:
            thread = await create_private_thread(user, "update")
        except Exception as e:
            await _safe_reply(interaction, f"Couldn't open your update thread right now ({e}). Please try again in a minute.")
            return
        _set_process_thread(user.id, token, thread.id)
        await _safe_reply(interaction, f"Your update thread is ready: {thread.mention}")
        await thread.send(existing_team_message, view=FlowCancelView(user.id))

        response = await ask_yes_no_question_in_thread(
            user, thread,
            "Are you sure you want to update your team?\nYou will need to add complete details again of each player if you continue.")
        if response == 'yes':
            try:
                await delete_team_from_sheet(user.id, constants.GOOGLE_SHEET_ID)
            except Exception as e:
                await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Error occurred while deleting team data from Google Sheets: {e}")
                _schedule_thread_delete(thread, 0)
                return

            await thread.send("Your previous team data was deleted, so even if you time out from here you will need to start enrollment fresh.\n\nA new enrollment thread is opening now. This thread closes in 15 seconds.")
            _schedule_thread_delete(thread, 15)
            thread = None
            await _release_process(user.id, token)
            await enrollTeam(user, interaction)
        else:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user} Update cancelled.")
            _schedule_thread_delete(thread, 0)

    except FlowCancelled:
        if thread is not None:
            try:
                await thread.send("Update cancelled. Your team was not changed.")
            except Exception:
                pass
        _schedule_thread_delete(thread, 5)
    except asyncio.TimeoutError:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Update timed out. Please try again later.")
        _schedule_thread_delete(thread, 0)
    except ValueError as ve:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An error occurred during update: {ve}")
        _schedule_thread_delete(thread, 0)
    except Exception as e:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An unexpected error occurred during update: {e}")
        _schedule_thread_delete(thread, 0)
    finally:
        await _release_process(user.id, token)
        _reset_enroll_menu(getattr(interaction, "message", None))

async def deleteTeam(user, existing_team_message, interaction):
    token = await _claim_process(user.id, "team delete")
    thread = None
    try:
        try:
            thread = await create_private_thread(user, "delete")
        except Exception as e:
            await _safe_reply(interaction, f"Couldn't open your delete thread right now ({e}). Please try again in a minute.")
            return
        _set_process_thread(user.id, token, thread.id)
        await _safe_reply(interaction, f"Your delete thread is ready: {thread.mention}")
        await thread.send(existing_team_message, view=FlowCancelView(user.id))

        response = await ask_yes_no_question_in_thread(
            user, thread,
            "Are you sure you want to delete your team?\nIt cant be reverted later on and all details from our end will be lost.")
        if response == 'yes':
            try:
                await delete_team_from_sheet(user.id, constants.GOOGLE_SHEET_ID)
            except Exception as e:
                await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Error occurred while deleting team data from Google Sheets: {e}")
                _schedule_thread_delete(thread, 0)
                return
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Your team was deleted successfully.")
        else:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user} Delete cancelled.")
        _schedule_thread_delete(thread, 0)

    except FlowCancelled:
        if thread is not None:
            try:
                await thread.send("Delete cancelled. Your team is safe.")
            except Exception:
                pass
        _schedule_thread_delete(thread, 5)
    except asyncio.TimeoutError:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Delete timed out. Please try again later.")
        _schedule_thread_delete(thread, 0)
    except ValueError as ve:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An error occurred during delete: {ve}")
        _schedule_thread_delete(thread, 0)
    except Exception as e:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An unexpected error occurred during delete: {e}")
        _schedule_thread_delete(thread, 0)
    finally:
        await _release_process(user.id, token)
        _reset_enroll_menu(getattr(interaction, "message", None))

class EnrollmentError(Exception):
    def __init__(self, timeout=300,text=""):
        self.timeout = timeout
        self.text = text


async def _wait_for_reply(user, channel, timeout, cancel_ev=None, check=None):
    """Wait for the user's next message in channel, or raise FlowCancelled
    as soon as the flow's Cancel button / Stop button fires."""
    if check is None:
        check = lambda msg: msg.author == user and msg.channel == channel
    waiter = asyncio.ensure_future(bot.wait_for('message', check=check, timeout=int(timeout)))
    if cancel_ev is None:
        return await waiter
    canceller = asyncio.ensure_future(cancel_ev.wait())
    try:
        await asyncio.wait({waiter, canceller}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (waiter, canceller):
            if not t.done():
                t.cancel()
    if cancel_ev.is_set():
        raise FlowCancelled()
    return waiter.result()  # re-raises asyncio.TimeoutError on timeout

async def get_user_response_in_thread(user, channel, prompt="", timeout=300, return_message_object=False,embed=None,return_first_member=False):
    if prompt or embed:
        await channel.send(prompt, embed=embed)
    cancel_ev = _flow_cancel_events.get(getattr(user, "id", None))
    while True:
        response = await _wait_for_reply(user, channel, timeout, cancel_ev)
        content = (response.content or "").strip()
        if cancel_ev is not None and content.lower() == "cancel":
            raise FlowCancelled()
        if return_first_member:
            if response.mentions:
                return response.mentions[0]
            await channel.send("Please @mention the user.")
            continue
        if not content:
            await channel.send("Please type your answer as a text message.")
            continue
        if return_message_object:
            return response
        return content

async def ask_yes_no_question_in_thread(user, channel, question, timeout=300):
    """Yes/no with one-tap buttons. Typing yes / no still works."""
    view = YesNoView(user.id, timeout=timeout)
    prompt = await channel.send(f"{question}\nTap a button (or type yes / no).", view=view)
    cancel_ev = _flow_cancel_events.get(user.id)

    def _check(msg):
        return (msg.author == user and msg.channel == channel
                and (msg.content or "").strip().lower() in ("yes", "no", "y", "n", "cancel"))

    text_waiter = asyncio.ensure_future(bot.wait_for('message', check=_check, timeout=timeout))
    button_waiter = asyncio.ensure_future(view.done.wait())
    waiters = {text_waiter, button_waiter}
    cancel_waiter = None
    if cancel_ev is not None:
        cancel_waiter = asyncio.ensure_future(cancel_ev.wait())
        waiters.add(cancel_waiter)
    try:
        await asyncio.wait(waiters, timeout=timeout + 5, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in waiters:
            if not t.done():
                t.cancel()

    async def _lock_buttons(choice=None):
        if view.choice is None:
            view.lock(choice)
            view.stop()
            try:
                await prompt.edit(view=view)
            except Exception:
                pass

    if cancel_ev is not None and cancel_ev.is_set():
        await _lock_buttons()
        raise FlowCancelled()
    if view.choice in ("yes", "no"):
        return view.choice
    if text_waiter.done() and not text_waiter.cancelled() and text_waiter.exception() is None:
        answer = (text_waiter.result().content or "").strip().lower()
        if answer == "cancel" and cancel_ev is not None:
            await _lock_buttons()
            raise FlowCancelled()
        choice = "yes" if answer in ("yes", "y") else "no"
        await _lock_buttons(choice)
        return choice
    await _lock_buttons()
    await channel.send("Response timed out. Please try again later.")
    return None

async def send_registration_details(user, team_name, player_igns, thread):
    await thread.send("Here's your team so far, copy it for the next step:")
    registration_details = f"## Team {team_name}\n"
    for ign in player_igns:
        registration_details += f"{ign} -\n"

    await thread.send(registration_details)

def _ordered_mentions(message, limit=5):
    """Mentioned members in the order they appear in the message text.
    Discord does not guarantee message.mentions order, and IGNs are paired
    with mentions by position, so order matters."""
    by_id = {m.id: m for m in message.mentions}
    ordered = []
    for uid in getattr(message, "raw_mentions", []) or []:
        m = by_id.get(uid)
        if m is not None and m not in ordered:
            ordered.append(m)
    for m in message.mentions:
        if m not in ordered:
            ordered.append(m)
    return ordered[:limit]

async def validate_enrollment(user, team_name, player_igns, thread):

    if not thread:
        print("Validation thread not found.")
        return False

    existing_team_message = ""
    embed = discord.Embed()
    embed.set_image(url="https://cdn.discordapp.com/attachments/1203357142548094977/1257239145752039474/download.gif?ex=66845772&is=668305f2&hm=dfa572f9b7382118aa8cca97415a5d425b7a994d4653cd0ae79689e796f8b171&")

    prompt = (f"`7/7`  Now mention every player next to their IGN, in the same order as above, like the example beneath, and send it here.\n"
              f"_Go ahead, mention your teammates now (including yourself)_")
    attempts = 0
    while True:
        attempts += 1
        response = await get_user_response_in_thread(user, thread, prompt, 600, True, embed=embed)  # 10 minutes
        embed = None
        players = _ordered_mentions(response, 5)
        if not players and attempts < 3:
            prompt = "I didn't see any @mentions in that message. Mention all players (including yourself) next to their IGNs and send it again."
            continue
        if players and len(players) != len(player_igns) and len(players) >= 4 and attempts < 3:
            await response.add_reaction("\u274c")
            prompt = (f"You entered {len(player_igns)} IGNs but mentioned {len(players)} players. "
                      f"Mention exactly {len(player_igns)} players, one per IGN, in the same order, and send it again.")
            continue
        break

    player_discord_ids = [str(p.id) for p in players]

    # Initiator must be one of the mentioned players
    if str(user.id) not in player_discord_ids:
        await thread.send(f"{user.mention} You must mention yourself as a teammate to enroll your team (4-5 players, including you).\nPlease restart the enrollment and mention all players including yourself.")
        await response.add_reaction("\u274c")
        raise EnrollmentError(60)

    # Check if any of the mentioned players are already enrolled
    for discord_id in player_discord_ids:

        if discord_id in constants.blk_users_list:
            await thread.send(f"Your enrollment can't proceed as user : <@{discord_id}> from your team is blacklisted as of now.")
            await response.add_reaction("\u274c")
            raise EnrollmentError(60)

        text = await isAlreadyEnrolled(discord_id)
        if text:
            existing_team_message += f"Your enrollment can't proceed as either You or One of your teammate is already a part of some other team:\n"
            existing_team_message += text
            existing_team_message += f"\nIf they're not a part of listed team, reach out to the support team via <#{constants.HELP_CHANNEL_ID}>."
            await thread.send(existing_team_message)
            await response.add_reaction("\u274c")
            raise EnrollmentError

    # Check if at least 4 users are mentioned
    if len(players) < 4:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Hey {user.mention}, you missed mentioning all your teammates.\nPlease restart the enrollment process and mention correctly next time.\nThis message can also be sent if someone from your team is not present in this server.")
        await response.add_reaction("\u274c")
        raise EnrollmentError(60)

    # All validation checks passed
    await response.add_reaction("\u2705")
    await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Enrollment for team {team_name} validated.")

    # Write enrollment details to Google Sheets
    await write_to_sheet(user.id, team_name, player_igns, player_discord_ids)
    await thread.send("\u2705 All set! Your team is enrolled and can book slots right away. This thread closes in 1 minute.")
    return True

# Function to write enrollment details to Google Sheets
async def write_to_sheet(initiator_id, team_name, player_igns, player_discord_ids):
    initiator_idstr = str(initiator_id)

    # Convert player_discord_ids to strings
    player_discord_ids = [str(discord_id) for discord_id in player_discord_ids]

    # Create a list to hold the values for each column in the correct sequence
    row = [initiator_idstr, team_name]
      
    # Add Discord usernames and IGNs in alternating sequence
    for discord_id, ign in zip(player_discord_ids, player_igns):
        row.extend([discord_id, ign])
    
    # Fill any remaining columns with empty strings
    remaining_columns = 12 - len(row)  # 12 is the total number of columns
    row.extend([''] * remaining_columns)
    
    # Append the row to the Google Sheets. gspread is blocking; run it off the
    # event loop so a stalled Google call can't freeze Discord's heartbeat.
    await asyncio.to_thread(constants.sheet.append_row, row)

    # Show the new team in the local cache immediately instead of after the
    # next 5 s refresh, so the team can book (and duplicates are blocked) now.
    try:
        with constants.cache_data_thread_lock:
            if constants.cached_data is not None:
                constants.cached_data = list(constants.cached_data) + [row]
    except Exception as e:
        print(f"[cache] local append failed (non-fatal): {e}")
    
    # Print registration details for verification
    print(f"Registered: {initiator_idstr}, Team: {team_name}, Discord Usernames: {', '.join(player_discord_ids)}, IGNs: {', '.join(player_igns)}")
    return row

async def delete_team_from_sheet(user_id, spreadsheet_id,ctx = None):
    """Delete the team row containing user_id. Returns the deleted row, or
    None if no row matched. Google API errors are raised so callers can tell
    the user instead of pretending it worked. Runs the blocking API calls off
    the event loop so the whole bot doesn't freeze while Google answers."""
    sheet = constants.service.spreadsheets()
    result = await asyncio.to_thread(lambda: sheet.values().get(spreadsheetId=spreadsheet_id, range="Sheet1").execute())
    values = result.get('values', [])

    # Find the row index and the row containing the user_id
    row_index, row = next(((i + 1, row) for i, row in enumerate(values) if str(user_id) in row), (None, None))

    if row_index is None:
        print("Team data not found for deletion.")
        return None

    request_body = {
        "requests": [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": 0,
                        "dimension": "ROWS",
                        "startIndex": row_index - 1,  # Subtracting 1 to match 0-based index in API
                        "endIndex": row_index
                    }
                }
            }
        ]
    }
    await asyncio.to_thread(lambda: sheet.batchUpdate(spreadsheetId=spreadsheet_id, body=request_body).execute())

    # Drop it from the local cache right away (refresh runs every 5 s). Without
    # this an update flow could reject the team's own old name as "taken".
    try:
        with constants.cache_data_thread_lock:
            if constants.cached_data is not None:
                constants.cached_data = [r for r in constants.cached_data if str(user_id) not in r]
    except Exception as e:
        print(f"[cache] local delete failed (non-fatal): {e}")

    try:
        if row and row[0] and str(row[0]).isdigit() and int(user_id) != int(row[0]):
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Hey <@{row[0]}> some of your teammate just deleted/updated your team. This is just an alert message(no need to worry) as the team was created by you.")
    except Exception as e:
        print(f"[delete] owner alert failed (non-fatal): {e}")

    print(f"Team data deleted successfully.\n{row}")
    return row

def _norm_team_name(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()

# Function to check if a team name already exists in the Google Sheets
async def is_team_name_unique(team_name):
    """Checked against the 5 s sheet cache (instant), case and spacing
    insensitive. Falls back to the sheet itself, off the event loop."""
    wanted = _norm_team_name(team_name)
    rows = constants.cached_data
    if rows:
        return all(_norm_team_name(r[1]) != wanted for r in rows if len(r) > 1)
    try:
        team_names = await asyncio.to_thread(constants.sheet.col_values, 2)
    except Exception as e:
        print(f"[unique] sheet read failed, reconnecting: {e}")
        json_keyfile_path = _google_key_path()
        constants.sheet, constants.service = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.GOOGLE_SHEET_ID)
        constants.ban_sheet, _ = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.BAN_SHEET_ID)
        constants.blacklist_sheet, _ = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.BLACKLIST_SHEET_ID)
        team_names = await asyncio.to_thread(constants.sheet.col_values, 2)
    return all(_norm_team_name(n) != wanted for n in team_names)

async def isAlreadyEnrolled(user_id,used2returnrow=False,returnTeamName=False,ctx_is_in_team = False,used2returnrowwithmessage = False):
    try:
        # Iterate through each row to find the user's team
        for row in constants.cached_data:
            # Check if the user's Discord ID is in the row
            if str(user_id) in row:
                if used2returnrow: return row[2::2]
                # Extract team details from the row
                team_name = row[1]
                player_igns = row[3::2]  # Player IGNs are in odd indices
                discord_ids = row[2::2]  # Discord IDs are in even indices

                # Construct a message with team details
                message = f"### {team_name}\n"

                if ctx_is_in_team:
                    for i, (name, discord_id) in enumerate(zip(player_igns[:4], discord_ids[:4]), 1):
                        message += f"{i}. {name}  \u00b7  <@{discord_id}>\n"

                    # Check for a fifth player
                    if len(discord_ids) > 4 and discord_ids[4] is not None:
                        fifth_ign = player_igns[4]
                        fifth_discord_id = discord_ids[4]
                        if fifth_ign and fifth_discord_id:
                            message += f"5. {fifth_ign}  \u00b7  <@{fifth_discord_id}>\n"

                else:
                    for i, discord_id in enumerate(discord_ids[:4], 1):
                        message += f"{i}. P{i}  \u00b7  <@{discord_id}>\n"

                    if len(discord_ids) >= 5 and discord_ids[4].strip():
                        message += f"5. P5  \u00b7  <@{discord_ids[4]}>\n"

                if returnTeamName:
                    return message, team_name
                elif used2returnrowwithmessage:
                    return message, row
                
                return message

        # If the user's team is not found, return None
        return None

    except Exception as e:
        print("Error occurred while checking Discord ID:", e)
        return None

def _active_entries(rows):
    """Rows are [key, start_unix, duration_seconds, ...] after a header row.
    Returns {key: end_unix or None}. Expired rows are skipped. A row whose
    time columns can't be read stays active, so hand-made rows still work."""
    now = time.time()
    out = {}
    for row in (rows or [])[1:]:
        if not row or not str(row[0]).strip():
            continue
        key = row[0]
        try:
            until = float(row[1]) + float(row[2])
            if until <= now:
                continue
        except (IndexError, ValueError, TypeError):
            until = None
        if key in out:
            out[key] = None if (out[key] is None or until is None) else max(out[key], until)
        else:
            out[key] = until
    return out

def _until_text(until):
    if not until:
        return ""
    end = datetime.datetime.fromtimestamp(until, tz=constants.timezone)
    return f" until {end.strftime('%d %b, %I:%M %p')} IST"

def _sheet_poller(name, get_sheet, list_attr, until_attr, lock, interval, active_only=True):
    """Keep a list in constants in sync with a Google sheet (runs in a thread)."""
    initialized = False
    while True:
        try:
            sheet = get_sheet()
            rows = sheet.get_all_values()
            if active_only:
                active = _active_entries(rows)
                with lock:
                    setattr(constants, list_attr, list(active.keys()))
                    setattr(constants, until_attr, active)
            else:
                with lock:
                    setattr(constants, list_attr, rows)
            if not initialized:
                print(f"\n{name} initialized.")
                initialized = True
        except Exception as e:
            print(f"Error occurred while refreshing {name}:", e)
        time.sleep(interval)

def refresh_cache():
    _sheet_poller("cache_data", lambda: constants.sheet, "cached_data", None, constants.cache_data_thread_lock, 5, active_only=False)

def refresh_cache2():
    _sheet_poller("banned_teams_list", lambda: constants.ban_sheet, "banned_team_list", "ban_until", constants.ban_list_thread_lock, 20)

def refresh_cache3():
    _sheet_poller("blk_users_list", lambda: constants.blacklist_sheet, "blk_users_list", "blk_until", constants.blk_list_thread_lock, 5)

def refresh_cache4():
    _sheet_poller("cd_users_list", lambda: constants.cooldown_sheet, "cd_team_list", "cd_until", constants.cd_list_thread_lock, 5)

async def lift_restriction(kind, target_text):
    """Remove active ban / cooldown / blacklist rows for a team or user.
    Returns (removed_count, what) and updates the local lists right away."""
    sheet = {"ban": constants.ban_sheet, "cooldown": constants.cooldown_sheet, "blacklist": constants.blacklist_sheet}[kind]
    ids = re.findall(r"\d{15,20}", target_text or "")
    if kind == "blacklist":
        if not ids:
            raise ValueError("Give an @mention or user ID for a blacklist.")
        key_match = lambda k: str(k).strip() == ids[0]
        what = f"<@{ids[0]}>"
    else:
        if ids:
            hits = _find_team_rows(ids[0], limit=1)
            if not hits:
                raise ValueError("That user isn't in any team.")
            want = _norm_team_name(hits[0][1])
            what = hits[0][1]
        else:
            want = _norm_team_name(target_text)
            what = target_text.strip()
        key_match = lambda k: _norm_team_name(k) == want
    rows = await asyncio.to_thread(sheet.get_all_values)
    idxs = [n + 1 for n, r in enumerate(rows) if n > 0 and r and key_match(r[0])]
    for idx in sorted(idxs, reverse=True):
        await asyncio.to_thread(sheet.delete_rows, idx)
    list_attr, until_attr, lock = {
        "ban": ("banned_team_list", "ban_until", constants.ban_list_thread_lock),
        "cooldown": ("cd_team_list", "cd_until", constants.cd_list_thread_lock),
        "blacklist": ("blk_users_list", "blk_until", constants.blk_list_thread_lock),
    }[kind]
    with lock:
        setattr(constants, list_attr, [k for k in getattr(constants, list_attr) if not key_match(k)])
        setattr(constants, until_attr, {k: v for k, v in getattr(constants, until_attr).items() if not key_match(k)})
    return len(idxs), what

async def validate_registration(user,check_cooldown = True,check_left_server = True, user_idd = None):
    try:
        if user == "None" and user_idd:
            user_id = user_idd
        else:
            user_id = user.id
        guild = bot.get_guild(int(constants.GUILD_ID))

        rows = constants.cached_data
        if not rows:
            # Cache not warmed up yet: read the sheet off the event loop.
            rows = await asyncio.to_thread(constants.sheet.get_all_values)
            with constants.cache_data_thread_lock:
                constants.cached_data = rows

        for row in rows:
            if str(user_id) in row:
                if check_left_server:
                    for discord_id in row[2::2]:  # Discord IDs are in even indices
                        if discord_id and str(discord_id).isdigit():
                            if guild is None or guild.get_member(int(discord_id)) is None:
                                return 'left_server'
                return row[1]

        # If the user's team is not found, return None
        return None
    except Exception as e:
        print("Error occurred while checking Discord ID:", e)
        return None
    
# Function to check the number of available slots
def available_slots(lobby_number):
    return constants.LOBBY_SIZE - len(constants.lobby_teams[int(lobby_number)-1])

def available_slots2(lobby_number):
    return constants.SPECIAL_LOBBY_SIZE - len(constants.special_lobby_teams[int(lobby_number)-1])


async def save_timestamp_to_csv(user, timestamp_ms,lobby_number,status : str):
    with open('timestamps.csv', 'a', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([user, timestamp_ms,f"Lobby {lobby_number}",status])

async def save_as_csv(teams_dict, csv_file, save_all_flag=False):
    with open(csv_file, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)

        if save_all_flag:
            # Write the header row
            writer.writerow(['Team_Name', 'User_IDS'])

            # Extract User IDs and team names from the dictionary
            team_names = [key for key in teams_dict.keys()]
            user_ids = [','.join(v) if isinstance(v, (list, tuple)) else '' for v in (teams_dict[team_name] for team_name in team_names)]

            # Write team names and corresponding user IDs to the CSV
            for team_name, user_id in zip(team_names, user_ids):
                writer.writerow([team_name, user_id])
        else:
            # Write the header row
            writer.writerow(['User_ID', 'Team_Name'])

            # Extract User IDs and team names from the dictionary
            user_ids = [key for key in teams_dict.keys()]
            team_names = [teams_dict[user_id] for user_id in user_ids]

            # Write user IDs and corresponding team names to the CSV
            for user_id, team_name in zip(user_ids, team_names):
                writer.writerow([user_id, team_name])

# Function to assign role to a user
async def assign_role(user, role_id):
    try:
        # Assign the role to the member
        await user.add_roles(bot.get_guild(constants.GUILD_ID).get_role(role_id))
    except Exception as e:
        print(f"An error occurred while assigning role to user {user}: {e}")


async def assign_team_to_lobby(user, lobby_number, t3=False):

    if t3:
        lobby_role = discord.utils.get(
            bot.get_guild(constants.GUILD_ID).roles,
            name=f"T3 G{lobby_number} IDP"
        )
    else:
        lobby_role = discord.utils.get(
            bot.get_guild(constants.GUILD_ID).roles,
            name=f"Group {lobby_number} IDP"
        )

    if lobby_role:
        await user.add_roles(lobby_role)

class AddTeamError(Exception):
    """A mod add-team request that can't be done; the message says why."""

def _reg_open(is_t3):
    return (not constants.special_disabled_status) if is_t3 else (not constants.disabled_status)

def _lobby_from_channel_name(name):
    """('group-3-idp' -> (3, False)), ('t3-idp-2' -> (2, True)), else None."""
    try:
        if name.startswith('t3-idp-'):
            return int(name.split('-')[-1]), True
        if name.startswith('group-') and name.endswith('-idp'):
            return int(name.split('-')[1]), False
    except Exception:
        pass
    return None

def _lobby_count(is_t3):
    if is_t3:
        return int(constants.SPECIAL_SLOTS_LIMIT) // int(constants.SPECIAL_LOBBY_SIZE)
    return int(constants.SLOTS_LIMIT) // int(constants.LOBBY_SIZE)

def _channel_ref(lobby_number, is_t3):
    ch = _idp_channel(lobby_number, is_t3)
    return ch.mention if ch else f"#{_idp_channel_name(lobby_number, is_t3)}"

def _idp_channel(lobby_number, is_t3):
    guild = bot.get_guild(constants.GUILD_ID)
    if guild is None:
        return None
    name = f"t3-idp-{lobby_number}" if is_t3 else f"group-{lobby_number}-idp"
    return discord.utils.get(guild.channels, name=name)

def get_lobby_teams(lobby_number, is_t3):
    """Teams in a lobby as {team_name: booker_id}. While registration is open
    the in-memory state is the truth (the JSON file is only written at close);
    after close the file is the truth (cancel/claim/add edit it)."""
    idx = int(lobby_number) - 1
    mem = constants.special_lobby_teams if is_t3 else constants.lobby_teams
    mem_dict = dict(mem[idx]) if 0 <= idx < len(mem) else {}
    if _reg_open(is_t3):
        return mem_dict
    fname = f"alt_lobby_{lobby_number}_teams.json" if is_t3 else f"lobby_{lobby_number}_teams.json"
    try:
        with open(fname, 'r') as f:
            return json.load(f)
    except Exception:
        return mem_dict

async def add_team_slotlist(team_name,member,channel, use_alt_lobby=None):

    team_name = str(team_name or "").strip()
    if not team_name:
        raise AddTeamError("Team name can't be empty.")

    # Auto-detect which lobby system based on channel name if not specified
    if use_alt_lobby is None:
        # RegistrationView3 uses 't3-idp-X' channel names
        # RegistrationView2 uses 'group-X-idp' channel names
        use_alt_lobby = channel.name.startswith('t3')

    # Extract the channel number from the matching channel name
    try:
        if use_alt_lobby:
            # Format: t3-idp-{number}
            channel_number = int(channel.name.split('-')[-1])
        else:
            # Format: group-{number}-idp
            channel_number = int(channel.name.split('-')[1])
    except (ValueError, IndexError):
        raise AddTeamError(f"{channel.mention} is not a lobby IDP channel (expected group-N-idp or t3-idp-N).")

    # While registration is running the lobby lives in memory and the JSON
    # file is rewritten from memory at close, so a team added to the file now
    # would silently vanish at close. Block it with a clear reason instead.
    if _reg_open(use_alt_lobby):
        kind = "T3" if use_alt_lobby else "Open"
        raise AddTeamError(f"{kind} registration is still running. Add Team unlocks as soon as it closes (lobbies full, auto-close, or /break).")

    # New team to add
    new_team = {team_name: member.id}

    # Use different JSON files for different registration views to avoid collision
    json_file_name = f"alt_lobby_{channel_number}_teams.json" if use_alt_lobby else f"lobby_{channel_number}_teams.json"

    try:
        with open(json_file_name, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}

    if team_name in data:
        raise AddTeamError(f"{team_name} is already in Lobby {channel_number}.")

    # check if this lobby has any cancelled slots, fill the first one instead of appending
    cancelled_position = None
    cancelled_key_found = None
    for i, (k, v) in enumerate(data.items()):
        if v == "cancelled":
            cancelled_position = i
            cancelled_key_found = k
            break

    if cancelled_position is not None:
        # replace the cancelled placeholder with the new team at the same position
        items = list(data.items())
        items[cancelled_position] = (team_name, member.id)
        data = dict(items)
    else:
        # no cancelled slots, append to end as usual
        data.update(new_team)

    _atomic_write_json(json_file_name, data)

    # if we filled a cancelled slot, mark it as claimed and delete the claim message
    if cancelled_key_found:
        async with constants.cancel_slots_lock:
            try:
                cancelled_data = load_cancelled_slots()
                for key, entry in cancelled_data.items():
                    if (entry.get("cancel_key") == cancelled_key_found
                        and entry["lobby"] == channel_number
                        and not entry.get("claimed")):
                        # mark as claimed
                        entry["claimed"] = True
                        entry["claimed_by"] = team_name
                        save_cancelled_slots(cancelled_data)

                        # delete the claim message since slot is now filled
                        try:
                            claim_channel = bot.get_channel(entry["channel_id"])
                            if claim_channel:
                                claim_msg = await claim_channel.fetch_message(entry["message_id"])
                                await claim_msg.delete()
                        except Exception as e:
                            print(f"Could not delete claim message: {e}")
                        break
            except Exception as e:
                print(f"Error updating cancelled_slots.json after mod add: {e}")

    team_names = list(data.keys())
    async with asyncio.TaskGroup() as taskhandler:
        try:
            _cd = (not constants.special_disabled_status) if use_alt_lobby else (not constants.disabled_status)
            # _cd True means registrations still open -> cancel stays disabled
            await send_slots_list(team_names, channel_number, channel, edit_slots_list=True, use_alt_lobby=use_alt_lobby, cancel_disabled=_cd)
        except Exception as e:
            print(f"Got Exception: {e}")
    
    # Assign the correct role based on channel type
    if use_alt_lobby:
        role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"T3 G{channel_number} IDP")
    else:
        role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"Group {channel_number} IDP")
    
    if role:
        await member.add_roles(role)

    # Record it like a booking so claim limits see it, and persist.
    try:
        group = get_group_for_lobby(channel_number, use_alt_lobby)
        if group:
            (constants.special_registered_set if use_alt_lobby else constants.registered_set).add((team_name, group))
        state_snapshot.save_state_snapshot()
    except Exception as e:
        print(f"[add-team] record failed (non-fatal): {e}")
    return channel_number

async def share_lobby_results(lobby_number, team_name):
    with open(f"lobby_{lobby_number}_teams.json", 'r') as f:
        teams_json = json.load(f)

        user_id = teams_json[team_name]

        t3_role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name= f"T3 Verified")
        member = bot.get_guild(constants.GUILD_ID).get_member(user_id)
        await member.add_roles(t3_role)

        channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).text_channels, name=f"group-{lobby_number}-idp")

        await channel.send(f"Results updated in <#{constants.RESULTS_CHANNEL_ID}>\n\n{team_name} : <@{user_id}>\n\nPromoted to Tier 3.")


async def send_slots_list(team_names, lobby_number, lobby_channel,edit_slots_list=  False, add_button = True, use_alt_lobby = False, cancel_disabled = False):
    # Prepare the slots list message
    slots_list_message = "```yaml\n"
    
    # Add the first two slots as "EMPTY" and "RESERVED"
    slots_list_message += f"01. EMPTY\n"
    slots_list_message += f"02. EMPTY\n"

    # Add the team names to the slots list
    for i, team_name in enumerate(team_names, start=3):
        formatted_index = f"{i:02}"  # Ensure two-digit format
        slots_list_message += f"{formatted_index}. {team_name}\n"

    # Add the remaining slots as "RESERVED"
    for i in range(len(team_names) + 3, 26):
        formatted_index = f"{i:02}"  # Ensure two-digit format
        slots_list_message += f"{formatted_index}. EMPTY\n"

    # Close the code block and send the slots list message to the lobby channel
    slots_list_message += f"```\n**Rules:**\n1. Make sure to checkout your lobbies schedule from the \"Tier-3 Schedule\" button in <#{constants.INFO_CHANNEL_ID}>.\n2. Be available on time and participate in all matches with minimum 3 players in lobbies to avoid a ban.\n3. All players' in-game names (IGN) must include a team acronym as a prefix/suffix (Similar NAME TAG). Players without this will not be allowed and kicked from the lobby.\n4. If there is an issue with changing IGN's (In Game Name), you can participate from a new id but have to ensure that raw pov is available.\n5. Use the button beneath in case you wanna transfer lobby role to teammate, it will be removed from you btw."
    if cancel_disabled:
        slots_list_message += "\n6. Cancel buttons open after registrations close."
    group = get_group_for_lobby(int(lobby_number), use_alt_lobby)
    label = _group_label(group, use_alt_lobby) if group else ""
    sched = (constants.match_schedule_t3 if use_alt_lobby else constants.match_schedule).get(int(lobby_number), {})
    title = f"{'T3 ' if use_alt_lobby else ''}GROUP {lobby_number} SLOTS LIST" + (f"  \u00b7  {label}" if label else "")
    embed = discord.Embed(title=title, description=slots_list_message, color=0x229db7)
    if sched:
        embed.add_field(name="Matches today", value=_match_lines(sched), inline=False)
        deadline = get_lobby_cancel_deadline(int(lobby_number), use_alt_lobby)
        if deadline:
            embed.add_field(name="Cancel closes", value=_when(deadline), inline=False)

    if edit_slots_list:
        # Prefer in-memory ids (live during registration); fall back to disk file.
        mem_dict = constants.temp_json_dict2 if use_alt_lobby else constants.temp_json_dict
        mem_entry = mem_dict.get(lobby_number) or mem_dict.get(str(lobby_number))
        if mem_entry:
            try:
                message = await bot.get_channel(mem_entry[1]).fetch_message(mem_entry[0])
                await message.edit(embed=embed,view=IdpChannelTasksView(cancel_disabled=cancel_disabled))
                return
            except Exception as e:
                print(f"Got Exception {e} while fetchin' live slot list message for lobby {lobby_number}.")
        json_file_name = 'lobby_details2.json' if use_alt_lobby else 'lobby_details.json'
        try:
            with open(json_file_name, 'r') as f:
                lobby_details_json = json.load(f)

            if lobby_details_json and str(lobby_number) in lobby_details_json:
                try:
                    message = await bot.get_channel(lobby_details_json[str(lobby_number)][1]).fetch_message(lobby_details_json[str(lobby_number)][0])
                    await message.edit(embed=embed,view=IdpChannelTasksView(cancel_disabled=cancel_disabled))
                    # adopt into memory so later live edits hit it
                    mem_dict[lobby_number] = [message.id, message.channel.id]
                    return
                except Exception as e:
                    print(f"Got Exception {e} while fetchin' slot list message for lobby {lobby_number}.")
            else:
                print(f"Lobby {lobby_number} not found in {json_file_name}, creating new message instead.")
        except FileNotFoundError:
            print(f"{json_file_name} not found, creating new message instead.")
        except Exception as e:
            print(f"Error reading {json_file_name}: {e}, creating new message instead.")

    message = await lobby_channel.send(embed=embed)
    
    # Use the appropriate role name based on which lobby system
    if use_alt_lobby:
        lobby_role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"T3 G{lobby_number} IDP")
    else:
        lobby_role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"Group {lobby_number} IDP")
    
    ping = lobby_role.mention if lobby_role else ""
    await lobby_channel.send(f"{ping} Your slot list is up above. Room IDs will be posted in this channel.\n\nEvery player's IGN must include your team tag, or they get kicked from the room. Playing from a new ID is fine, as long as it has the tag.")
    try:
        if add_button:
            await message.edit(view=IdpChannelTasksView(cancel_disabled=cancel_disabled))
    except Exception as e:
        print(e)
    
    # Use the appropriate dictionary based on the parameter
    if use_alt_lobby:
        constants.temp_json_dict2[lobby_number] = [message.id, lobby_channel.id]
    else:
        constants.temp_json_dict[lobby_number] = [message.id, lobby_channel.id]

async def init_sheet():

    json_keyfile_path = _google_key_path()

    try:
        # Attempt to connect to Google Sheets
        constants.sheet, constants.service = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.GOOGLE_SHEET_ID)
        constants.ban_sheet, _ = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.BAN_SHEET_ID)
        constants.blacklist_sheet, _ = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.BLACKLIST_SHEET_ID)
        constants.cooldown_sheet, _ = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.COOLDOWN_SHEET_ID)
    except Exception as e:
        print("Error while connecting to Google Sheets:", e)

    # Start a separate thread to periodically refresh the cache
    refresh_thread = threading.Thread(target=refresh_cache)
    refresh_thread.daemon = True
    refresh_thread.start()

    # Start a separate thread to periodically refresh the ban list
    refresh_thread2 = threading.Thread(target=refresh_cache2)
    refresh_thread2.daemon = True
    refresh_thread2.start()

    # Start a separate thread to periodically refresh the ban list
    refresh_thread3 = threading.Thread(target=refresh_cache3)
    refresh_thread3.daemon = True
    refresh_thread3.start()

    # Start a separate thread to periodically refresh the ban list
    refresh_thread4 = threading.Thread(target=refresh_cache4)
    refresh_thread4.daemon = True
    refresh_thread4.start()

if __name__ == "__main__":

    with asyncio.Runner() as runner:
        runner.run(init_sheet())
        
    # Run the bot with the specified token
    # basicConfig above already logs to stderr (and so to bot_logs.txt);
    # log_handler=None stops discord.py adding a second handler (doubled lines).
    bot.run(os.environ.get('DISCORD_TOKEN'), log_handler=None)