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

class BatchedPrintLogger:
    def __init__(self, filename, batch_size=50):
        self.terminal = sys.stdout
        self.filename = filename
        self.buffer = []
        self.batch_size = batch_size

    def write(self, message):
        self.terminal.write(message)
        self.buffer.append(message)
        if len(self.buffer) >= self.batch_size:
            self.flush()

    def flush(self):
        if self.buffer:
            with open(self.filename, "a", encoding="utf-8") as f:
                f.write("".join(self.buffer))
            self.buffer.clear()

sys.stdout = BatchedPrintLogger("bot_logs.txt")

# Configure logging
logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# Create a new instance of the Discord bot with command functionality
bot = commands.Bot(command_prefix='-', intents=intents)

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
        # Handle the interaction response based on the selected value
        selected_value = self.values[0]
        user = interaction.user

        # Check if a process is already running
        if constants.running_processes.get(user.id):
            await interaction.response.send_message("Another process is already running. Please wait until the previous process is finished (up to 2 minutes of inactivity).", ephemeral=True,delete_after=15)
            return
        
        if str(user.id) in constants.blk_users_list:
            await interaction.response.send_message(f"Hey {user.mention} you are blacklisted as of now.", ephemeral=True,delete_after=30)
            await interaction.message.edit(view=TournamentView())
            return
        
        # Check if the user is already enrolled
        result = await isAlreadyEnrolled(user.id,returnTeamName=True,ctx_is_in_team=True)
        
        if selected_value == "enroll":
            
            if result:
                existing_team_message = f"{user.mention} Your enrollment can't proceed as either You or One of your teammates is already a part of some other team:\n"
                existing_team_message += result[0]
                existing_team_message += f"\nIf they're not a part of the listed team, reach out to the support team via <#{constants.HELP_CHANNEL_ID}>."
                await interaction.response.send_message(existing_team_message, ephemeral=True,delete_after=150)
                await interaction.message.edit(view=TournamentView())
                return
            
            # elif not any(role.name == constants.REQUIRED_ROLE_NAME for role in user.roles):
            #     await interaction.response.send_message(f"Hey {user.mention} you are not verified yet, please complete that first.\nIf verified ho and still this pops up, claim your verification role from <#{constants.TICKET_CHANNEL_ID}>", ephemeral=True,delete_after=30)
            #     await interaction.message.edit(view=TournamentView())
            #     return
            
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(interaction.response.send_message("Enrollment Started, Check your mentions!", ephemeral=True, delete_after=10))
                task_group.create_task(enrollTeam(user,interaction))
                task_group.create_task(interaction.message.edit(view=TournamentView()))

        elif selected_value == "update":

            if not result:
                await interaction.response.send_message(f"{user.mention} You aren't enrolled in any team as of now. Please select 'Enroll' to create one.", ephemeral=True,delete_after=15)
                await interaction.message.edit(view=TournamentView())
                return
            
            if result[1] in constants.banned_team_list:
                await interaction.response.send_message(f"Sorry Mate {user.mention}, You can't update your team while it is banned.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",ephemeral=True,delete_after=40)
                await interaction.message.edit(view=TournamentView())
                return
            
            if result[1] in constants.cd_team_list:
                await interaction.response.send_message(f"Sorry Mate {user.mention}, You can't update your team while it is under cooldown.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",ephemeral=True,delete_after=40)
                await interaction.message.edit(view=TournamentView())
                return

            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(interaction.response.send_message("Update selected, Check your mentions!", ephemeral=True, delete_after=10))
                task_group.create_task(updateTeam(user,result[0],interaction))
                task_group.create_task(interaction.message.edit(view=TournamentView()))

        elif selected_value == "delete":

            if not result:
                await interaction.response.send_message(f"{user.mention} You aren't enrolled in any team as of now. Please select 'Enroll' to create one.", ephemeral=True,delete_after=15)
                await interaction.message.edit(view=TournamentView())
                return
            
            if result[1] in constants.banned_team_list:
                await interaction.response.send_message(f"Sorry Mate {user.mention}, You can't delete your team while it is banned.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",ephemeral=True,delete_after=30)
                await interaction.message.edit(view=TournamentView())
                return
            
            if result[1] in constants.cd_team_list:
                await interaction.response.send_message(f"Sorry Mate {user.mention}, You can't delete your team while it is under cooldown.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",ephemeral=True,delete_after=40)
                await interaction.message.edit(view=TournamentView())
                return
            
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(interaction.response.send_message("Delete selected, Check your mentions!", ephemeral=True, delete_after=10))
                task_group.create_task(deleteTeam(user,result[0],interaction))
                task_group.create_task(interaction.message.edit(view=TournamentView()))

# Define the TournamentView class inheriting from discord.ui.View
class TournamentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Set timeout to None to make the view persistent
        self.add_item(TournamentDropdown())

async def send_selectmenu(channel):
    # embed = discord.Embed(title="Team Enrollment", description="Select desirable option to create or edit your team details->\n\n a. \"**Enroll**\" team to pull the ropes now, introduce your crew nd here we sail!\n\n b. \"**Update**\" your team if you are just fed of someone midway.\n\n c. \"**Delete**\" your team if stuck on some lonesome island.\n\nUse of alt accounts/Fake mentions are strictly prohibited and as you'll Get caught will be titled a Good Enough BAN 🙃.\n\n**Atleast 4 players from your team must be verified.\n\nWait until previous process is timed out (that'll take upto 2 mins of inactivity) in case you wanna redo/alter your selection", color=0x229db7)
    embed = discord.Embed(title="Team Enrollment", description="Select desirable option to create or edit your team details->\n\n a. **\"Enroll\"** -> CREATE NEW TEAM\n\n b. **\"Update\"** -> UPDATE YOUR TEAM\n\n c. **\"Delete\"** -> DELETE YOUR TEAM\n\nUse of alt accounts/Fake mentions are strictly prohibited and as you'll Get caught will be titled a Good Enough BAN.\n\n*> Atleast 4 players from your team should have **\"Verified\"** role on discord.*\nWait until previous process is timed out (that'll take upto 2 mins of inactivity) in case you wanna redo/alter your selection", color=0x229db7)    
    view = TournamentView()  # Initialize TournamentView with timeout=None
    message = await channel.send(embed=embed, view=view)
    return message

async def send_pref_menu(channel):
    embed = discord.Embed(title="Lobby Preferences", description=f"*Hey Wanderer, can I lurk on you :>*\n\nSet your lobby preferences from the dropdown below if you are able to play at a particular time shift only.\n\nYou can select a maximum of 3 lobbies at one instance.", color=0x229db7)
    view = LobbyPreferencesView()
    message = await channel.send(embed=embed, view=view)
    return message

def reg_base_description(is_t3=False):
    enroll = constants.ENROLLMENT_CHANNEL_ID
    if is_t3:
        return f'''**OPENS AT 12 PM**

1. Make sure that you have completed the enrollment of your team from this channel <#{enroll}>
2. Please book a slot only if you wanna participate in the scrims, there wont be any slot cancellation/reassignment later on.
3. Fastest ones to register in any lobby will be allocated with the slots.
4. You need to pass in a simple Captcha test for registration, have a look at it anytime with 'TRIAL REG' button.
5. One team can play up to 2 lobbies per day.'''
    return f'''*Hey Wanderer, can I lurk on you :>*

**OPENS AT 12 PM**

1. Make sure that you have completed the enrollment of your team from this channel <#{enroll}>
2. Please book a slot only if you wanna participate in the scrims, there wont be any slot cancellation/reassignment later on.
3. Fastest ones to register in any lobby will be allocated with the slots.
4. You need to pass in a simple Captcha test for registration, have a look at it anytime with 'TRIAL REG' button.
5. One team can play only 1 lobby per group, so a team can register in both groups.'''

def reg_slot_lines(is_t3=False):
    if is_t3:
        lobby_map = constants.GROUP_LOBBY_MAP2
        teams = constants.special_lobby_teams
        size = int(constants.SPECIAL_LOBBY_SIZE)
    else:
        lobby_map = constants.GROUP_LOBBY_MAP
        teams = constants.lobby_teams
        size = int(constants.LOBBY_SIZE)
    lines = []
    for group, lobbies in lobby_map.items():
        for n in lobbies:
            idx = int(n) - 1
            filled = len(teams[idx]) if 0 <= idx < len(teams) else 0
            lines.append(f'{group} Lobby {n} ({filled}/{size})')
    sep = chr(10) + chr(10)
    return sep + chr(10).join(lines)

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
        desc = reg_base_description(is_t3)
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

# async def send_overview_menu(channel):
#     view = ScrimsOverviewView()  
#     message = await channel.send(content=f"Hey there, here's a complete overview of BGMI scrims at Trident:\n\nThere are 4 tiers basically,\n\n`Trident Rookie Scrims(Tier 3):`\n\n- Open for all, anyone can participate, registrations open at 12 PM Tuesday-Saturday in <#{constants.REGISTRATION_CHANNEL_ID}>\n- 8 Groups every day, Top 1 from each Group qualify for Amateur Scrims\n- Every Group plays 2 matches, Erangle-Miramar\n\n`Amateur Scrims(T2 filtration):`\n\n- 2 Groups on Sunday, Top 4 from each Group qualify for Tier 2 scrims\n- Every Group plays 3 matches, Erangle-Miramar-Sanhok\n\n`Trident Elite Scrims(Tier 2):`\n\n- 4 Groups, Every team plays 24 matches over 6 days.\n- Tuesday-Sunday daily 4 matches, Erangle-Miramar-Sanhok-Vikendi\n- Top 6 teams based on cumulative leaderboard of both Groups qualify for Pro Scrims.\n- Bottom 12 teams are demoted from Tier 2 to Tier 3.\n\n`Trident Pro Scrims:`\n\n- Tuesday-Sunday daily 4 matches, Erangle-Miramar-Sanhok-Vikendi\n- Top 6 teams based on leaderboard retain their spots in Pro Scrims.\n- Rest of the teams are demoted from Pro Scrims to Tier 2.\n\nAny announcements and updates would be shared thru the <#1188849982632099940> channels.\nReact with buttons beneath for more information and make sure to follow all rules.",view=view)
#     return message

async def send_overview_menu(channel):
    view = ScrimsOverviewView() 
    message = await channel.send(content="""## Trident BGMI Scrims 

---

**1. Trident Rookie Scrims (Tier 3):**  
- **Open for all:** Anyone with <@&1217461189152608277> role can participate.  
- **Registration:** Opens at 12 PM (Tuesday-Saturday) in **<#1252296674739490908>**.  
- **Format:**  
  - 8 Groups daily.  
  - Each Group plays **2 matches**.  
  - **Top 1** team from each Group qualifies for Amateur Scrims.  

---

**2. Amateur Scrims (Tier 2 Filtration):**  
- **Day:** Sunday.  
- **Format:**  
  - 2 Groups.  
  - Each Group plays **3 matches**.  
  - **Top 4 teams** from each Group qualify for Tier 2 Scrims.  

---

**3. Trident Elite Scrims (Tier 2):**  
- **Format:**  
  - 4 Groups, each team plays **18 matches** across 6 days (Tuesday-Sunday).  
  - **Daily matches:** 3 matches/day.  
  - **Qualification:**  
    - **Top 9 teams** (cumulative leaderboard) qualify for Tier 1 Scrims.  
    - **Bottom 12 teams** are demoted to T3.  

---

**4. Trident Tier 1 Scrims:**  
- **Format:**  
  - **27 Teams** split into **3 Groups** (A, B, C) with **9 teams each**.  
  - (9 qualified from T2 + 9 Invited + 9 teams from previous week's T1 scrims) 
  - Play in a **Round-Robin format** over 3 days (Tuesday-Thursday).  
  - **Total matches:** 9 matches, with each group playing **6 matches**.  
  - **Qualification:**  
    - **Top 9 teams** qualify for Pro Scrims and next week’s Tier 1 Scrims.  
    - **Bottom 18 teams** are demoted to T2.  

---

**5. Trident Pro Scrims:**  
- **Format:**  
  - **Top 9 teams** from Tier 1 compete with **9 invited professional teams**.  
  - 3 matches/day across 3 days (Friday-Sunday).  
  - Teams earn points for the **Trident BGMI Overall Leaderboard**.  

---

Any announcements and updates would be shared thru the ⁠<#1188849982632099940> channels.
React with buttons beneath for more information and make sure to follow all rules.""",view=view)
    return message

class LobbySelectDropdown(discord.ui.Select):
    def __init__(self,disabled = False):
        options = [discord.SelectOption(label=f"Group {i}", value=f"{int(i)}", emoji="🌟") for i in range(1, (int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE)) + 1)]
        super().__init__(placeholder="Select you lobby preferences here", min_values=1, max_values=3, options=options,disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        constants.preferences_dict[user.id] = tuple(self.values)
        await interaction.response.send_message(f"Lobby Preferences updated: {', '.join(self.values)}", ephemeral=True,delete_after=30)
        await interaction.message.edit(view=LobbyPreferencesView())

class CheckPreferencesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Current Preferences", style=discord.ButtonStyle.green)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        if constants.preferences_dict.get(user.id):
            await interaction.response.send_message(f"Current preferences are set to: {', '.join(constants.preferences_dict.get(user.id))}", ephemeral=True,delete_after=60)
        else:
            await interaction.response.send_message(f"Current preferences are set to: None, your chances for confirmed slot are maxmimum! {user.mention}", ephemeral=True,delete_after=60)

class ClearPreferencesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Clear Preferences", style=discord.ButtonStyle.red)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        if user.id in constants.preferences_dict:
            del constants.preferences_dict[user.id]
        await interaction.response.send_message("Preferences cleared.", ephemeral=True,delete_after=15)
        await interaction.message.edit(view=LobbyPreferencesView())

class LobbyPreferencesView(discord.ui.View):
    def __init__(self,disabled = False):
        super().__init__(timeout=None)
        self.add_item(LobbySelectDropdown(disabled = disabled))
        self.add_item(CheckPreferencesButton())
        self.add_item(ClearPreferencesButton())
        
class LobbyButton(discord.ui.Button):
    def __init__(self, lobby_number):
        super().__init__(label=f'Lobby {lobby_number}', style=discord.ButtonStyle.green,disabled=constants.special_disabled_status,emoji=f"{constants.emotes_list[lobby_number-1]}",row = 1 if lobby_number >= 5 else 0)
        # row = int(lobby_number + 1) - 2 if lobby_number % 2 == 1 else lobby_number - 2
        self.lobby_number = lobby_number

    async def callback(self, interaction: discord.Interaction):
        # Check if the lobby has available slots
        user_id = interaction.user.id
        user = interaction.user
        team_name = await validate_registration(user)
        
        if team_name:
            if team_name in constants.banned_team_list:
                await interaction.response.send_message(f"{user.mention} Someone from your team is banned at the moment.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",ephemeral=True,delete_after=60)

            # elif team_name == 'cooldown':
            #     await interaction.response.send_message(f"{user.mention} Someone from your team is on cooldown, please wait for the cooldown period to end\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",ephemeral=True,delete_after=30)

            elif team_name in constants.cd_team_list:
                await interaction.response.send_message(f"{user.mention} Someone from your team is on cooldown, please wait for the cooldown period to end\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",ephemeral=True,delete_after=60)

            elif team_name == 'left_server':
                await interaction.response.send_message(f"{user.mention} Someone from your team is not present in this server rn.",ephemeral=True,delete_after=60)

            elif team_name in constants.special_registered_teams.keys():
                await interaction.response.send_message("Someone from your team has already booked a slot for today.", ephemeral=True,delete_after=120)
            
            elif available_slots2(self.lobby_number) > 0:
                await interaction.response.send_modal(CaptchaModal(self.lobby_number,team_name))
                
            else:
                await interaction.response.send_message("Sorry, this lobby is full.", ephemeral=True,delete_after=10)
        
        else: await interaction.response.send_message(f"You are not a part of any team right now, please ask your IGL or yourself enlist your team from <#{constants.ENROLLMENT_CHANNEL_ID}>.", ephemeral=True,delete_after=60)

class CaptchaModal(discord.ui.Modal):
    def __init__(self, lobby_number, team_name):
        super().__init__(title="Let's fill in a captcha real quick!")
        self.lobby_number = lobby_number
        self.team_name = team_name
        self.slots_available = True
        self.already_registered = False

        # Add the captcha input fields
        self.sentence_input = discord.ui.TextInput(label=f"Type this beneath:\n{constants.captcha_question_variables[0]}", placeholder=constants.captcha_question_variables[0],required=True)
        self.sum1_input = discord.ui.TextInput(label=f"{constants.captcha_question_variables[1]} + {constants.captcha_question_variables[2]}", placeholder="Answer this easyyyy summation 1",required=True)
        self.sum2_input = discord.ui.TextInput(label=f"{constants.captcha_question_variables[3]} + {constants.captcha_question_variables[4]}", placeholder="Answer this easyyyy summation 2",required=True)
        
        self.add_item(self.sentence_input)
        self.add_item(self.sum1_input)
        self.add_item(self.sum2_input)

    async def on_submit(self, interaction: discord.Interaction):
        
        user = interaction.user
        user_id = interaction.user.id

        if await validate_captcha(self.sentence_input.value.rstrip(),int(self.sum1_input.value.rstrip()),int(self.sum2_input.value.rstrip())):

            await interaction.response.defer(ephemeral=True,thinking=True)  # Defer the interaction response

            timestamp_ms = datetime.datetime.now(tz=constants.timezone).strftime("%b %d %H:%M:%S.%f")

            # # Acquire the registration lock for this lobby
            # async with constants.lobby_locks[int(int(self.lobby_number) - 1)]:
            #     slots_available_currently = available_slots(self.lobby_number)
            #     if int(slots_available_currently) <= 0:
            #         await interaction.followup.send("Sorry, this lobby is full.", ephemeral=True) 
            #         await save_timestamp_to_csv(interaction.user, timestamp_ms,self.lobby_number,"LATE")
            #         return
                
            #     if self.team_name in constants.registered_teams.keys():
            #         await interaction.followup.send("Someone from your team has already booked a slot for today.", ephemeral=True)
            #         return
                
            #     # Save the registered team's data
            #     constants.registered_teams[self.team_name] = await isAlreadyEnrolled(user_id,used2returnrow=True)
            #     constants.lobby_teams[int(self.lobby_number)-1][user_id] = self.team_name

            # Acquire the registration lock for this lobby
            async with constants.special_lobby_locks[int(int(self.lobby_number) - 1)]:
                
                slots_available_currently = available_slots2(self.lobby_number)

                if int(slots_available_currently) <= 0:
                    self.slots_available = False
                
                elif self.team_name in constants.special_registered_teams.keys() and self.slots_available:
                    self.already_registered = True
                
                elif self.slots_available and not self.already_registered:
                    # Save the registered team's data
                    constants.special_registered_teams[self.team_name] = await isAlreadyEnrolled(user_id,used2returnrow=True)
                    constants.special_lobby_teams[int(self.lobby_number)-1][self.team_name] = user_id

            if not self.slots_available:
                await interaction.followup.send("Sorry, this lobby is full.", ephemeral=True) 
                await save_timestamp_to_csv(interaction.user, timestamp_ms,self.lobby_number,"LATE")
                return
            
            if self.already_registered:
                await interaction.followup.send("Someone from your team has already booked a slot for today.", ephemeral=True)
                return
                
            # Operations that do not need to be locked

            task1 = asyncio.create_task(interaction.followup.send(f"Registration confirmed for “{self.team_name}” in Lobby {self.lobby_number}.", ephemeral=True))
            # task2 = asyncio.create_task(assign_role(user, constants.COOLDOWN_ROLE_ID))
            task3 = asyncio.create_task(assign_team_to_lobby(user, self.lobby_number, True))
            task4 = asyncio.create_task(save_timestamp_to_csv(user, timestamp_ms, self.lobby_number,"BOOKED"))

            # await asyncio.gather(task1,task2,task3,task4)
            await asyncio.gather(task1,task3,task4)
            try:
                await refresh_live_slot_list(self.lobby_number, is_t3=True)
            except Exception as e:
                print(f"[live-slots] refresh failed (non-fatal): {e}")
            try:
                state_snapshot.save_state_snapshot()
            except Exception as e:
                print(f"[snapshot] save failed (non-fatal): {e}")

            async with constants.special_registration_lock:
                if len(constants.special_registered_teams) == constants.SPECIAL_SLOTS_LIMIT:
                    if not constants.special_disabled_status:
                        constants.special_disabled_status = True
                        message = await bot.get_channel(constants.SPECIAL_REGISTRATION_CHANNEL_ID).fetch_message(constants.SPECIAL_REG_MESSAGE_ID)
                        await message.edit(view=RegistrationView())
                        await save_as_csv(constants.special_registered_teams, 'registered_teams.csv',save_all_flag = True)
                        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File('registered_teams.csv'))

                        for lobby_number, lobby_teams_dict in enumerate(constants.special_lobby_teams, 1):
                            # csv_file = f"lobby_{lobby_number}_teams.csv"
                            # await save_as_csv(lobby_teams_dict, csv_file)

                            json_file_name = f"alt_lobby_{lobby_number}_teams.json"
                            # Write the data dictionary to a JSON file
                            with open(json_file_name, 'w') as f:
                                json.dump(lobby_teams_dict, f, indent=1)

                            team_names = list(lobby_teams_dict.keys())
                            user_ids = [lobby_teams_dict[team_name] for team_name in team_names]
                            async with asyncio.TaskGroup() as taskhandler:
                                # taskhandler.create_task(bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File(csv_file)))
                                taskhandler.create_task(bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File(json_file_name)))
                                try:
                                    idp_channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"t3-idp-{lobby_number}")
                                    await send_slots_list(team_names, lobby_number,idp_channel, add_button=True, use_alt_lobby=True, edit_slots_list=True, cancel_disabled=False)
#                                     pov_message = """Hello Teams,

# Please follow these steps to record your Point of View (POV) while playing BGMI:
# 1. Before opening the BGMI app show the list of background running apps on your device
# 2. Go to the PlayStore (for Android users) or Appstore (for iOS users) and open the BGMI app.
# 3. Join the Lobby using the provided details.
# 4. Before starting the match, ensure that both your in-game audio and your own voice (microphone) are being recorded.
# 5. Play the match.
# 6. After each match, make sure to show the list of background running apps and IMEI on your device.
# 7. You need to repeat the above steps for every match you play."""
#                                     await idp_channel.send(pov_message)
                                except Exception as e:
                                    print(f"Got Exception when sending lobby csv files: {e}")
                        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"You can download the Google Sheets app to view the list of users and their registration timestamps of {datetime.datetime.today().strftime('%d %b')} from this CSV file (for transparency). If you cant find you name in these, you were later than all these 😢.",file=discord.File('timestamps.csv'))
                        
                        with open('lobby_details.json','w') as json_file:
                            json.dump(constants.temp_json_dict,json_file,indent=1)
                        try:
                            await refresh_reg_message(is_t3=True, show_slots=False, force=True)
                        except Exception as e:
                            print('[reg-slots] hide failed (non-fatal):', e)

            print("Registration confirmed for user:", user_id)
            print(f"Available slots in Lobby {self.lobby_number}:", int(slots_available_currently)-1)

        else:
            await interaction.response.send_message("Invalid captcha 😢 GG! Please try again later.", ephemeral=True, delete_after=30)
    
    async def on_error(self, interaction: discord.Interaction, error: Exception):
        if isinstance(error, ValueError):
            await interaction.response.send_message("Summation answers can be numbers only.", ephemeral=True,delete_after=240)
        else:
            await interaction.response.send_message("Network died either on your or our end. Please Try again later", ephemeral=True,delete_after=240)
            print(f"An error occurred during registeration for {interaction.user}: {error}")


class RegistrationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(1, (int(constants.SPECIAL_SLOTS_LIMIT/constants.SPECIAL_LOBBY_SIZE)) + 1):
            self.add_item(LobbyButton(i))
        self.add_item(PracticeRegistrationButton())

class PracticeRegistrationButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'TRIAL REG', style=discord.ButtonStyle.primary,emoji=constants.practice_emoteid)

    async def callback(self, interaction: discord.Interaction):
        team_name = await validate_registration(interaction.user)
        if team_name:
            await interaction.response.send_modal(PracticeRegistrationModal())
        else: await interaction.response.send_message(f"You are not a part of any team right now, please ask your IGL or yourself enlist your team from <#{constants.ENROLLMENT_CHANNEL_ID}>.", ephemeral=True,delete_after=60)

class PracticeRegistrationModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Let's fill in a captcha real quick!")
        self.word = ''.join(random.choice(ascii_lowercase) for _ in range(random.randint(5, 7)))
        self.nums = [random.randint(10,99) for _ in range(4)]
        
        # Add the captcha input fields
        self.sentence_input = discord.ui.TextInput(label=f"Type this beneath:\n{self.word}", placeholder=self.word,required=True)
        self.sum1_input = discord.ui.TextInput(label=f"{self.nums[0]} + {self.nums[1]}", placeholder="Answer this easyyyy summation 1",required=True)
        self.sum2_input = discord.ui.TextInput(label=f"{self.nums[2]} + {self.nums[3]}", placeholder="Answer this easyyyy summation 2",required=True)
    
        self.add_item(self.sentence_input)
        self.add_item(self.sum1_input)
        self.add_item(self.sum2_input)

    async def on_submit(self, interaction: discord.Interaction):
        if (self.sentence_input.value.rstrip().lower() == self.word and int(self.sum1_input.value.rstrip()) == int(self.nums[0] + self.nums[1]) and int(self.sum2_input.value.rstrip()) == int(self.nums[2] + self.nums[3])):
            await interaction.response.send_message("Captcha Passed!", ephemeral=True,delete_after=15)
        else: await interaction.response.send_message("Invalid captcha 😢 GG! Please try again later.", ephemeral=True, delete_after=30)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        if isinstance(error, ValueError):
            await interaction.response.send_message("Summation answers can be numbers only", ephemeral=True,delete_after=240)
        else:
            await interaction.response.send_message("Network died either on your or our end. Please Try again later", ephemeral=True,delete_after=240)
            print(f"An error occurred during practice session for {interaction.user}: {error}")
        

class RegistrationView2(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for group in constants.GROUP_LOBBY_MAP.keys():
            self.add_item(GroupButton(group))
        self.add_item(PracticeRegistrationButton())

class GroupButton(discord.ui.Button):
    def __init__(self, group):

        super().__init__(
            label=f"Grp {group} - {constants.GROUP_LABELS.get(group, '')}",
            style=discord.ButtonStyle.green,
            disabled=constants.disabled_status,
        )
        self.group = group

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        user_id = interaction.user.id
        team_name = await validate_registration(user)

        if not team_name:
            return await interaction.response.send_message(
                f"You are not a part of any team right now, please ask your IGL or yourself enlist your team from <#{constants.ENROLLMENT_CHANNEL_ID}>.",
                ephemeral=True, delete_after=60
            )
        if team_name in constants.banned_team_list:
            return await interaction.response.send_message(
                f"{user.mention} Someone from your team is banned at the moment.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",
                ephemeral=True, delete_after=60
            )
        if team_name in constants.cd_team_list:
            return await interaction.response.send_message(
                f"{user.mention} Someone from your team is on cooldown, please wait for the cooldown period to end\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",
                ephemeral=True, delete_after=60
            )
        if team_name == 'left_server':
            return await interaction.response.send_message(
                f"{user.mention} Someone from your team is not present in this server rn.",
                ephemeral=True, delete_after=60
            )
        # check: already registered in this specific group?
        if (team_name, self.group) in constants.registered_set:
            return await interaction.response.send_message(
                f"Someone from your team booked a slot in Group {self.group}.",
                ephemeral=True, delete_after=120
            )

        # check: max group registration limit (e.g. 2 groups max overall)
        groups_registered = 0
        for entry in constants.registered_set:
            if entry[0] == team_name:
                groups_registered += 1
        if groups_registered >= constants.MAX_GROUP_REGISTRATIONS:
            return await interaction.response.send_message(
                f"Your team has already registered in {groups_registered} groups (max {constants.MAX_GROUP_REGISTRATIONS}).",
                ephemeral=True, delete_after=120
            )

        # quick pre-check, any slot available in this group at all?
        group_lobbies = constants.GROUP_LOBBY_MAP[self.group]
        has_slot = False

        for lobby in group_lobbies:
            if available_slots(lobby) > 0:
                has_slot = True
                break

        if not has_slot:
            return await interaction.response.send_message(
                f"Group {self.group} is full.",
                ephemeral=True,
                delete_after=10,
            )

        await interaction.response.send_modal(
            GroupCaptchaModal(self.group, team_name)
        )

class GroupCaptchaModal(discord.ui.Modal):
    def __init__(self, group, team_name):
        super().__init__(title="Let's fill in a captcha real quick!")
        self.group = group
        self.team_name = team_name
        self.slots_available = True
        self.already_registered = False

        self.sentence_input = discord.ui.TextInput(
            label=f"Type this beneath:\n{constants.captcha_question_variables[0]}",
            placeholder=constants.captcha_question_variables[0], required=True
        )
        self.sum1_input = discord.ui.TextInput(
            label=f"{constants.captcha_question_variables[1]} + {constants.captcha_question_variables[2]}",
            placeholder="Answer this easyyyy summation 1", required=True
        )
        self.sum2_input = discord.ui.TextInput(
            label=f"{constants.captcha_question_variables[3]} + {constants.captcha_question_variables[4]}",
            placeholder="Answer this easyyyy summation 2", required=True
        )
        self.add_item(self.sentence_input)
        self.add_item(self.sum1_input)
        self.add_item(self.sum2_input)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        user_id = interaction.user.id

        if not await validate_captcha(
            self.sentence_input.value.rstrip(),
            int(self.sum1_input.value.rstrip()),
            int(self.sum2_input.value.rstrip())
        ):
            return await interaction.response.send_message(
                "Invalid captcha 😢 GG! Please try again later.", ephemeral=True, delete_after=30
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        timestamp_ms = datetime.datetime.now(tz=constants.timezone).strftime("%b %d %H:%M:%S.%f")
        group_lobbies = constants.GROUP_LOBBY_MAP[self.group]
        assigned_lobby = None

        async with constants.group_locks[self.group]:
            # re-check after lock: same group
            if (self.team_name, self.group) in constants.registered_set:
                self.already_registered = True
            # re-check after lock: max group limit (prevents race conditions)
            elif sum(1 for entry in constants.registered_set if entry[0] == self.team_name) >= constants.MAX_GROUP_REGISTRATIONS:
                self.already_registered = True
            else:
                for lobby in group_lobbies:
                    slots = available_slots(lobby)
                    if int(slots) > 0:
                        assigned_lobby = lobby
                        break
                if not assigned_lobby:
                    self.slots_available = False
                else:
                    row = await isAlreadyEnrolled(user_id, used2returnrow=True)
                    if not row:
                        return await interaction.followup.send(
                            "Your team was changed (deleted/updated) while you were solving the captcha, so this booking was not saved. Please book again.", ephemeral=True
                        )
                    constants.registered_teams[self.team_name] = row
                    constants.registered_set.add((self.team_name, self.group))
                    constants.lobby_teams[assigned_lobby - 1][self.team_name] = user_id

        if self.already_registered:
            return await interaction.followup.send(
                "Someone from your team has already booked a slot for today.", ephemeral=True
            )
        if not self.slots_available:
            await save_timestamp_to_csv(user, timestamp_ms, group_lobbies[0], "LATE")
            return await interaction.followup.send(
                f"Sorry, Group {self.group} is full.", ephemeral=True
            )


        task1 = asyncio.create_task(
            interaction.followup.send(
                f'Registration confirmed for "{self.team_name}" in Group {self.group} → Lobby {assigned_lobby}.',
                ephemeral=True
            )
        )
        task3 = asyncio.create_task(assign_team_to_lobby(user, assigned_lobby))
        task4 = asyncio.create_task(save_timestamp_to_csv(user, timestamp_ms, assigned_lobby, "BOOKED"))
        await asyncio.gather(task1, task3, task4)
        try:
            await refresh_live_slot_list(assigned_lobby, is_t3=False)
        except Exception as e:
            print(f"[live-slots] refresh failed (non-fatal): {e}")
        try:
            state_snapshot.save_state_snapshot()
        except Exception as e:
            print(f"[snapshot] save failed (non-fatal): {e}")

        async with constants.registration_lock:
            if len(constants.registered_set) >= constants.SLOTS_LIMIT:
                print("REGISTRATION FULL")
                if not constants.disabled_status:
                    constants.disabled_status = True
                    message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
                    await message.edit(view=RegistrationView2())
                    await save_as_csv(constants.registered_teams, 'registered_teams.csv', save_all_flag=True)
                    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File('registered_teams.csv'))
                    for lobby_number, lobby_teams_dict in enumerate(constants.lobby_teams, 1):
                        json_file_name = f"lobby_{lobby_number}_teams.json"
                        with open(json_file_name, 'w') as f:
                            json.dump(lobby_teams_dict, f, indent=1)
                        team_names = list(lobby_teams_dict.keys())
                        async with asyncio.TaskGroup() as taskhandler:
                            taskhandler.create_task(
                                bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File(json_file_name))
                            )
                            try:
                                idp_channel = discord.utils.get(
                                    bot.get_guild(constants.GUILD_ID).channels,
                                    name=f"group-{lobby_number}-idp"
                                )
                                await send_slots_list(team_names, lobby_number, idp_channel, edit_slots_list=True, cancel_disabled=False)
                            except Exception as e:
                                print(f"Got Exception when sending lobby csv files: {e}")
                    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
                        f"You can download the Google Sheets app to view the list of users and their registration timestamps of {datetime.datetime.today().strftime('%d %b')} from this CSV file (for transparency). If you cant find you name in these, you were later than all these 😢.",
                        file=discord.File('timestamps.csv')
                    )
                    with open('lobby_details.json', 'w') as json_file:
                        json.dump(constants.temp_json_dict, json_file, indent=1)
                    try:
                        await refresh_reg_message(is_t3=False, show_slots=False, force=True)
                    except Exception as e:
                        print('[reg-slots] hide failed (non-fatal):', e)

        print("Registration confirmed for user:", user_id)
        print(f"Assigned Lobby {assigned_lobby} in Group {self.group}")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        if isinstance(error, ValueError):
            await interaction.response.send_message(
                "Summation answers can be numbers only.", ephemeral=True, delete_after=240
            )
        else:
            await interaction.response.send_message(
                "Network died either on your or our end. Please Try again later",
                ephemeral=True, delete_after=240
            )
            print(f"An error occurred during registration for {interaction.user}: {error}")

class RegistrationView3(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for group in constants.GROUP_LOBBY_MAP2.keys():
            self.add_item(GroupButton2(group))
        self.add_item(PracticeRegistrationButton())

class GroupButton2(discord.ui.Button):
    def __init__(self, group):

        super().__init__(
            label=f"Grp {group} - {constants.GROUP_LABELS2[group]}",
            style=discord.ButtonStyle.green,
            disabled=constants.special_disabled_status,
        )
        self.group = group

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        user_id = interaction.user.id
        team_name = await validate_registration(user)

        if not team_name:
            return await interaction.response.send_message(
                f"You are not a part of any team right now, please ask your IGL or yourself enlist your team from <#{constants.ENROLLMENT_CHANNEL_ID}>.",
                ephemeral=True, delete_after=60
            )
        if team_name in constants.banned_team_list:
            return await interaction.response.send_message(
                f"{user.mention} Someone from your team is banned at the moment.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",
                ephemeral=True, delete_after=60
            )
        if team_name in constants.cd_team_list:
            return await interaction.response.send_message(
                f"{user.mention} Someone from your team is on cooldown, please wait for the cooldown period to end\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.",
                ephemeral=True, delete_after=60
            )
        if team_name == 'left_server':
            return await interaction.response.send_message(
                f"{user.mention} Someone from your team is not present in this server rn.",
                ephemeral=True, delete_after=60
            )
        if any(role.name.startswith("Amateur") for role in user.roles):
            return await interaction.response.send_message(
                f"{user.mention} You can't register here as you have an Amateur role.",
                ephemeral=True, delete_after=60
            )
        if (team_name, self.group) in constants.special_registered_set:
            return await interaction.response.send_message(
                f"Someone from your team booked a slot in Group {self.group}.",
                ephemeral=True, delete_after=120
            )

        # check: max group registration limit (2 lobbies max per day)
        t3_groups_registered = 0
        for entry in constants.special_registered_set:
            if entry[0] == team_name:
                t3_groups_registered += 1
        if t3_groups_registered >= constants.MAX_T3_GROUP_REGISTRATIONS:
            return await interaction.response.send_message(
                f"Your team has already registered in {t3_groups_registered} group(s) (max {constants.MAX_T3_GROUP_REGISTRATIONS}, 2 lobbies max per day).",
                ephemeral=True, delete_after=120
            )

        # quick pre-check, any slot available in this group at all?
        group_lobbies = constants.GROUP_LOBBY_MAP2[self.group]
        has_slot = False

        for lobby in group_lobbies:
            if available_slots2(lobby) > 0:
                has_slot = True
                break

        if not has_slot:
            return await interaction.response.send_message(
                f"Group {self.group} is full.",
                ephemeral=True,
                delete_after=10,
            )

        await interaction.response.send_modal(
            GroupCaptchaModal2(self.group, team_name)
        )

class GroupCaptchaModal2(discord.ui.Modal):
    def __init__(self, group, team_name):
        super().__init__(title="Let's fill in a captcha real quick!")
        self.group = group
        self.team_name = team_name
        self.slots_available = True
        self.already_registered = False

        self.sentence_input = discord.ui.TextInput(
            label=f"Type this beneath:\n{constants.captcha_question_variables[0]}",
            placeholder=constants.captcha_question_variables[0], required=True
        )
        self.sum1_input = discord.ui.TextInput(
            label=f"{constants.captcha_question_variables[1]} + {constants.captcha_question_variables[2]}",
            placeholder="Answer this easyyyy summation 1", required=True
        )
        self.sum2_input = discord.ui.TextInput(
            label=f"{constants.captcha_question_variables[3]} + {constants.captcha_question_variables[4]}",
            placeholder="Answer this easyyyy summation 2", required=True
        )
        self.add_item(self.sentence_input)
        self.add_item(self.sum1_input)
        self.add_item(self.sum2_input)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        user_id = interaction.user.id

        if not await validate_captcha(
            self.sentence_input.value.rstrip(),
            int(self.sum1_input.value.rstrip()),
            int(self.sum2_input.value.rstrip())
        ):
            return await interaction.response.send_message(
                "Invalid captcha 😢 GG! Please try again later.", ephemeral=True, delete_after=30
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        timestamp_ms = datetime.datetime.now(tz=constants.timezone).strftime("%b %d %H:%M:%S.%f")
        group_lobbies = constants.GROUP_LOBBY_MAP2[self.group]
        assigned_lobby = None

        async with constants.group_locks2[self.group]:
            # re-check after lock
            if (self.team_name, self.group) in constants.special_registered_set:
                self.already_registered = True
            # re-check after lock: max group limit (prevents race conditions)
            elif sum(1 for entry in constants.special_registered_set if entry[0] == self.team_name) >= constants.MAX_T3_GROUP_REGISTRATIONS:
                self.already_registered = True

            else:
                for lobby in group_lobbies:
                    slots = available_slots2(lobby)
                    if int(slots) > 0:
                        assigned_lobby = lobby
                        break
                if not assigned_lobby:
                    self.slots_available = False
                else:
                    row = await isAlreadyEnrolled(user_id, used2returnrow=True)
                    if not row:
                        return await interaction.followup.send(
                            "Your team was changed (deleted/updated) while you were solving the captcha, so this booking was not saved. Please book again.", ephemeral=True
                        )
                    constants.special_registered_teams[self.team_name] = row
                    constants.special_registered_set.add((self.team_name, self.group))
                    constants.special_lobby_teams[assigned_lobby - 1][self.team_name] = user_id

        if self.already_registered:
            return await interaction.followup.send(
                "Someone from your team booked a slot in Group " + self.group + ".", ephemeral=True
            )
        if not self.slots_available:
            await save_timestamp_to_csv(user, timestamp_ms, group_lobbies[0], "LATE")
            return await interaction.followup.send(
                f"Sorry, Group {self.group} is full.", ephemeral=True
            )

        # lobby filled notification
        task1 = asyncio.create_task(
            interaction.followup.send(
                f'Registration confirmed for "{self.team_name}" in Group {self.group} → Lobby {assigned_lobby}.',
                ephemeral=True
            )
        )
        task3 = asyncio.create_task(assign_team_to_lobby(user, assigned_lobby, True))
        task4 = asyncio.create_task(save_timestamp_to_csv(user, timestamp_ms, assigned_lobby, "BOOKED"))
        await asyncio.gather(task1, task3, task4)
        try:
            await refresh_live_slot_list(assigned_lobby, is_t3=True)
        except Exception as e:
            print(f"[live-slots] refresh failed (non-fatal): {e}")
        try:
            state_snapshot.save_state_snapshot()
        except Exception as e:
            print(f"[snapshot] save failed (non-fatal): {e}")

        async with constants.special_registration_lock:
            if len(constants.special_registered_set) >= constants.SPECIAL_SLOTS_LIMIT:
                print("REGISTRATION FULL")
                if not constants.special_disabled_status:
                    constants.special_disabled_status = True
                    message = await bot.get_channel(constants.SPECIAL_REGISTRATION_CHANNEL_ID).fetch_message(constants.SPECIAL_REG_MESSAGE_ID)
                    await message.edit(view=RegistrationView3())
                    await save_as_csv(constants.special_registered_teams, 'registered_teams.csv', save_all_flag=True)
                    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File('registered_teams.csv'))
                    for lobby_number, lobby_teams_dict in enumerate(constants.special_lobby_teams, 1):
                        json_file_name = f"alt_lobby_{lobby_number}_teams.json"
                        with open(json_file_name, 'w') as f:
                            json.dump(lobby_teams_dict, f, indent=1)
                        team_names = list(lobby_teams_dict.keys())
                        async with asyncio.TaskGroup() as taskhandler:
                            taskhandler.create_task(
                                bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File(json_file_name))
                            )
                            try:
                                idp_channel = discord.utils.get(
                                    bot.get_guild(constants.GUILD_ID).channels,
                                    name=f"t3-idp-{lobby_number}"
                                )
                                await send_slots_list(team_names, lobby_number, idp_channel, use_alt_lobby=True, edit_slots_list=True, cancel_disabled=False)
                            except Exception as e:
                                print(f"Got Exception when sending lobby csv files: {e}")
                    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
                        f"You can download the Google Sheets app to view the list of users and their registration timestamps of {datetime.datetime.today().strftime('%d %b')} from this CSV file (for transparency). If you cant find you name in these, you were later than all these 😢.",
                        file=discord.File('timestamps.csv')
                    )
                    with open('lobby_details2.json', 'w') as json_file:
                        json.dump(constants.temp_json_dict2, json_file, indent=1)
                    try:
                        await refresh_reg_message(is_t3=True, show_slots=False, force=True)
                    except Exception as e:
                        print('[reg-slots] hide failed (non-fatal):', e)

        print("Registration confirmed for user:", user_id)
        print(f"Assigned Lobby {assigned_lobby} in Group {self.group}")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        if isinstance(error, ValueError):
            await interaction.response.send_message(
                "Summation answers can be numbers only.", ephemeral=True, delete_after=240
            )
        else:
            await interaction.response.send_message(
                "Network died either on your or our end. Please Try again later",
                ephemeral=True, delete_after=240
            )
            print(f"An error occurred during registration for {interaction.user}: {error}")

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

class bitsInfoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f"I'm Verified but can not create team", row = 1,style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Follow the Step 2 listed above.",ephemeral=True,delete_after=30)

class bitsMoreInfoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f"Time Taken for Verification", row = 1,style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Website Verification is a manual process and may take upto 7-14 days, you need to wait until its done.",ephemeral=True,delete_after=30)

class bitFewInfoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f"Cross ❌arha even when Verified", row = 2,style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"One or more of your teammates haven't verified on the discord server yet.\nJust ask your teammates to complete the process listed above.\n(All 4 players of your team must be verified from your team in order to play)",ephemeral=True,delete_after=45)

class FaqView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ExampleSsButton())
        self.add_item(CheckVerificationButton())
        self.add_item(bitInfoButton())
        # self.add_item(bitsInfoButton())
        self.add_item(bitsMoreInfoButton())
        # self.add_item(bitFewInfoButton())

class TransferIDPButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Transfer IDP role', style=discord.ButtonStyle.grey)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        matching_roles = [role for role in interaction.user.roles if role.name in constants.idp_role_names]

        if len(matching_roles) == 1:
            try:
                result = await isAlreadyEnrolled(interaction.user.id,used2returnrowwithmessage=True,ctx_is_in_team=True)
                if result:
                    await interaction.followup.send(f"{result[0]}\nSelect player in the dropdown below whom you wanna transfer this IDP role to.",view=PlayerSelectView(row = result[1],role = matching_roles[0]),ephemeral=True)
                else:
                    await interaction.followup.send(f"Unable to retrieve any team associated with you, did you deleted it?",ephemeral=True)
            except Exception as e:
                print(e)
                await interaction.followup.send(f"Some error occured at our end.",ephemeral=True)

        elif len(matching_roles) == 0:
            await interaction.followup.send(f"You dont Got any role that can be transferred:.",ephemeral=True)

        else:
            await interaction.followup.send(f"You have like more than one Lobbies roles, i am comfused and can not handle role transfers for you.",ephemeral=True)

class PlayerSelectDropdown(discord.ui.Select):
    def __init__(self,row,role):
        options = []
        dc_ids = row[2::2]
        igns = row[3::2]
        self.role = role
        for i, ign in enumerate(igns, 1):
            if ign and dc_ids[i-1]:  # Ensure both ign and dc_id are not None or empty
                options.append(discord.SelectOption(label=f"{i}. {ign}", value=f"{dc_ids[i-1]}"))
        super().__init__(placeholder="Select here", options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_value = int(self.values[0])
        user = interaction.user
        try:
            await interaction.response.defer(ephemeral=True)
            benificar = bot.get_guild(constants.GUILD_ID).get_member(selected_value)
            await user.remove_roles(self.role)
            await asyncio.sleep(2)
            await assign_role(benificar,self.role.id)
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Hey {user.mention}, Your lobby role has been transferred to your teammate {benificar}")
            await interaction.followup.send("Done.", ephemeral=True)
            
        except discord.errors.Forbidden:
            await interaction.followup.send("I do not have permission to manage your roles.", ephemeral=True)
        except discord.errors.HTTPException:
            await interaction.followup.send("Failed to remove role due to a Discord API error.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Got some error: {e}", ephemeral=True)

class ModToolsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Mod Tools', style=discord.ButtonStyle.grey)

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.guild_permissions.manage_roles:

            try:
                await interaction.response.send_message("👀",view=ModToolsView(),ephemeral=True,delete_after=18)
            except Exception as e:
                await interaction.response.send_message(f"Got an Error: {e}",ephemeral=True)

        else:
            await interaction.response.send_message(f"You can't use this command my bruhh.",ephemeral=True,delete_after=40)

class TeamInfoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Teams Info', style=discord.ButtonStyle.grey)

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.guild_permissions.manage_roles:

            await interaction.response.send_message(f"on itt...",ephemeral=True,delete_after=3)

            # Extract the channel number from the matching channel name
            # Handle both 'group-X-idp' and 't3-idp-X' formats
            channel_name = interaction.channel.name
            if channel_name.startswith('t3-idp-'):
                # Format: t3-idp-{number} - use alt_lobby for RegistrationView3
                lobby_number_int = int(channel_name.split('-')[-1])
                json_file_name = f"alt_lobby_{lobby_number_int}_teams.json"
            else:
                # Format: group-{number}-idp or any other format - use normal lobby files
                lobby_number_int = int(channel_name.split('-')[1])
                json_file_name = f"lobby_{lobby_number_int}_teams.json"
            
            if 1 <= lobby_number_int <= (int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)):
                
                temp_dict =  None

                try:
                    with open(json_file_name, 'r') as f:
                        temp_dict = json.load(f)
                except FileNotFoundError:
                    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send("No teams registered in this lobby yet.")
                    return

                message = ""

                for team_name, user_id in temp_dict.items():
                    message += f"{team_name} -> <@{user_id}>\n"

                await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(message)

        else:
            await interaction.response.send_message(f"You can't use this command my bruhh.",ephemeral=True,delete_after=40)

class CopyTeamNamesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Copy TeamNames', style=discord.ButtonStyle.grey)

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.guild_permissions.manage_roles:

            await interaction.response.send_message(f"on itt...",ephemeral=True,delete_after=1)

            # Extract the channel number from the matching channel name
            # Handle both 'group-X-idp' and 't3-idp-X' formats
            channel_name = interaction.channel.name
            if channel_name.startswith('t3-idp-'):
                # Format: t3-idp-{number} - use alt_lobby for RegistrationView3
                lobby_number_int = int(channel_name.split('-')[-1])
                json_file_name = f"alt_lobby_{lobby_number_int}_teams.json"
            else:
                # Format: group-{number}-idp or any other format - use normal lobby files
                lobby_number_int = int(channel_name.split('-')[1])
                json_file_name = f"lobby_{lobby_number_int}_teams.json"
            
            if 1 <= lobby_number_int <= (int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)):
                
                temp_dict =  None

                try:
                    with open(json_file_name, 'r') as f:
                        temp_dict = json.load(f)
                except FileNotFoundError:
                    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send("No teams registered in this lobby yet.")
                    return

                message = ""

                for team_name, user_id in temp_dict.items():
                    message += f"{team_name}\n"

                await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(message)

        else:
            await interaction.response.send_message(f"You can't use this command my bruhh.",ephemeral=True,delete_after=40)

class AddTeamButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'Add Team', style=discord.ButtonStyle.grey)

    async def callback(self, interaction: discord.Interaction):

        if any(role.name in constants.roles_for_bot_access for role in interaction.user.roles):

            await interaction.response.send_message(f"on itt...",ephemeral=True,delete_after=3)
            try:
                mod_channel = bot.get_channel(constants.UPDATES_CHANNEL_ID) 
                team_name = await get_user_response_in_thread(
                    interaction.user,
                    mod_channel,
                    f"{interaction.user.mention} let me know the team's name."
                )

                member = await get_user_response_in_thread(
                    interaction.user,
                    mod_channel,
                    f"{interaction.user.mention} mention the user you wanna add.",return_first_member=True
                )

                await add_team_slotlist(team_name,member,interaction.channel)
                await mod_channel.send("kay")

            except asyncio.TimeoutError:
                pass
            except Exception as e:
                await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Got an Exception: {e}")
        else:
            await interaction.response.send_message(f"You can't use this command my bruhh.",ephemeral=True,delete_after=40)
    
class IdpChannelTasksView(discord.ui.View):
    def __init__(self, cancel_disabled=False):
        super().__init__(timeout=None)
        self.add_item(TransferIDPButton())
        self.add_item(ModToolsButton())
        self.add_item(CancelSlotButton(disabled=cancel_disabled))

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
    with open('cancelled_slots.json', 'w') as f:
        json.dump(data, f, indent=1)

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
                f"Cancel closed for Lobby {lobby_number} at {_fmt_ist(dl)} IST "
                f"(30 mins before M1 start {_fmt_ist(st)} IST).",
                ephemeral=True, delete_after=30
            )

        # find the team in the lobby JSON
        json_file_name = f"alt_lobby_{lobby_number}_teams.json" if is_t3 else f"lobby_{lobby_number}_teams.json"
        try:
            with open(json_file_name, 'r') as f:
                lobby_data = json.load(f)
        except FileNotFoundError:
            return await interaction.response.send_message("Lobby data not found.", ephemeral=True, delete_after=15)

        # find user's team in this lobby
        team_name = None
        for tn, uid in lobby_data.items():
            if int(uid) == user.id:
                team_name = tn
                break

        if not team_name:
            return await interaction.response.send_message(
                "You are not registered in this lobby.", ephemeral=True, delete_after=15
            )

        # show confirmation modal
        await interaction.response.send_modal(
            CancelConfirmModal(team_name, lobby_number, group, is_t3)
        )


class CancelConfirmModal(discord.ui.Modal):
    """Confirmation modal - user must type their team name to confirm cancellation."""
    def __init__(self, team_name, lobby_number, group, is_t3):
        super().__init__(title="Confirm Slot Cancellation")
        self.team_name = team_name
        self.lobby_number = lobby_number
        self.group = group
        self.is_t3 = is_t3

        self.confirm_input = discord.ui.TextInput(
            label=f'Type "{team_name}" to confirm',
            placeholder=team_name,
            required=True
        )
        self.add_item(self.confirm_input)

    async def on_submit(self, interaction: discord.Interaction):
        # validate confirmation text
        if self.confirm_input.value.strip() != self.team_name:
            return await interaction.response.send_message(
                "Team name didn't match. Cancellation aborted.", ephemeral=True, delete_after=15
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        async with constants.cancel_slots_lock:
            # re-check deadline inside lock (modal solving takes time)
            if is_lobby_started(self.lobby_number, self.is_t3):
                return await interaction.followup.send(
                    f"Match for Lobby {self.lobby_number} has already started. Cancel closed.",
                    ephemeral=True,
                )
            if not is_before_lobby_cancel_deadline(self.lobby_number, self.is_t3):
                dl = get_lobby_cancel_deadline(self.lobby_number, self.is_t3)
                return await interaction.followup.send(
                    f"Cancel closed for Lobby {self.lobby_number} at {_fmt_ist(dl)} IST.",
                    ephemeral=True,
                )
            # remove team from lobby JSON file
            json_file_name = f"alt_lobby_{self.lobby_number}_teams.json" if self.is_t3 else f"lobby_{self.lobby_number}_teams.json"
            try:
                with open(json_file_name, 'r') as f:
                    lobby_data = json.load(f)
            except FileNotFoundError:
                return await interaction.followup.send("Lobby data not found.", ephemeral=True)

            if self.team_name not in lobby_data:
                return await interaction.followup.send("Your team was not found in this lobby.", ephemeral=True)

            # replace the team entry with a CANCELLED placeholder at the same position
            # this keeps the dict the same size so no other teams shift position
            cancel_key = "CANCELLED"
            counter = 1
            while cancel_key in lobby_data:
                counter += 1
                cancel_key = f"CANCELLED ({counter})"

            items = list(lobby_data.items())
            position = next(i for i, (k, v) in enumerate(items) if k == self.team_name)
            items[position] = (cancel_key, "cancelled")
            lobby_data = dict(items)

            with open(json_file_name, 'w') as f:
                json.dump(lobby_data, f, indent=1)

            # remove the IDP role from the user
            if self.is_t3:
                role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"T3 G{self.lobby_number} IDP")
            else:
                role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"Group {self.lobby_number} IDP")

            if role:
                try:
                    await interaction.user.remove_roles(role)
                except Exception as e:
                    print(f"Error removing IDP role: {e}")

            # update the slot list embed in the IDP channel
            await update_slot_list_after_cancel(self.lobby_number, self.is_t3)

            # build embed for the claim message in registration channel
            reg_type = "T3" if self.is_t3 else "Open"
            claim_embed = discord.Embed(
                title="🔔 Slot Available!",
                description=(
                    f"**{self.team_name}** cancelled their slot.\n\n"
                    f"**Type:** {reg_type} Registration\n"
                    f"**Group:** {self.group}\n"
                    f"**Lobby:** {self.lobby_number}\n\n"
                    f"Click the button below to claim this slot."
                ),
                color=0x2ecc71
            )
            claim_embed.set_footer(text=f"Cancelled by {interaction.user.display_name}")

            # send claim message to the appropriate registration channel
            reg_channel_id = constants.SPECIAL_REGISTRATION_CHANNEL_ID if self.is_t3 else constants.REGISTRATION_CHANNEL_ID
            reg_channel = bot.get_channel(reg_channel_id)

            claim_view = ClaimSlotView(self.lobby_number, self.group, self.is_t3)
            claim_message = await reg_channel.send(embed=claim_embed, view=claim_view)

            # save to cancelled_slots.json for restart persistence
            cancelled_data = load_cancelled_slots()
            slot_key = f"{self.lobby_number}_{self.group}_{int(datetime.datetime.now(tz=constants.timezone).timestamp())}"
            cancelled_data[slot_key] = {
                "team_name": self.team_name,
                "lobby": self.lobby_number,
                "group": self.group,
                "type": "t3" if self.is_t3 else "open",
                "message_id": claim_message.id,
                "channel_id": reg_channel_id,
                "claimed": False,
                "cancel_key": cancel_key
            }
            save_cancelled_slots(cancelled_data)
            try:
                state_snapshot.save_state_snapshot()
            except Exception as e:
                print(f"[snapshot] save failed (non-fatal): {e}")
            # one-shot timer: delete this claim message exactly at M1 START
            try:
                schedule_claim_expiry(slot_key, self.lobby_number, self.is_t3)
            except Exception as e:
                print(f"[expire] schedule failed (non-fatal): {e}")

        await interaction.followup.send(
            f"Your slot in Group {self.group}, Lobby {self.lobby_number} has been cancelled.\n"
            f"A claim message has been posted in the registration channel.",
            ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await interaction.response.send_message(
            "Something went wrong during cancellation. Please try again.",
            ephemeral=True, delete_after=30
        )
        print(f"Error in CancelConfirmModal: {error}")


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

        # validate: team exists, not banned, etc.
        team_name = await validate_registration(user)
        if not team_name:
            return await interaction.response.send_message(
                f"You are not a part of any team right now, please enlist your team from <#{constants.ENROLLMENT_CHANNEL_ID}>.",
                ephemeral=True, delete_after=60
            )
        if team_name in constants.banned_team_list:
            return await interaction.response.send_message(
                f"{user.mention} Someone from your team is banned at the moment.",
                ephemeral=True, delete_after=60
            )
        if team_name in constants.cd_team_list:
            return await interaction.response.send_message(
                f"{user.mention} Someone from your team is on cooldown.",
                ephemeral=True, delete_after=60
            )
        if team_name == 'left_server':
            return await interaction.response.send_message(
                f"{user.mention} Someone from your team is not present in this server.",
                ephemeral=True, delete_after=60
            )

        # No time check for claims: if the message is there and the click
        # is valid, the team gets the slot. Expiry is handled by deleting
        # the message at match start (expire_stale_claim_messages).
        # show confirmation modal
        await interaction.response.send_modal(
            ClaimConfirmModal(self.lobby_number, self.group, self.is_t3, team_name, interaction.message)
        )


class ClaimConfirmModal(discord.ui.Modal):
    """Simple confirmation modal for claiming a slot."""
    def __init__(self, lobby_number, group, is_t3, team_name, claim_message):
        super().__init__(title="Confirm Slot Claim")
        self.lobby_number = lobby_number
        self.group = group
        self.is_t3 = is_t3
        self.team_name = team_name
        self.claim_message = claim_message

        self.confirm_input = discord.ui.TextInput(
            label=f'Type "confirm" to claim this slot',
            placeholder="confirm",
            required=True
        )
        self.add_item(self.confirm_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirm_input.value.strip().lower() != "confirm":
            return await interaction.response.send_message(
                "You didn't type 'confirm'. Claim aborted.", ephemeral=True, delete_after=15
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        async with constants.cancel_slots_lock:
            # No time check for claims: message existence + valid click = slot.
            # check if slot is still available (not already claimed)
            cancelled_data = load_cancelled_slots()
            slot_entry = None
            slot_key = None

            for key, entry in cancelled_data.items():
                if (entry["lobby"] == self.lobby_number
                    and entry["group"] == self.group
                    and entry["type"] == ("t3" if self.is_t3 else "open")
                    and entry["message_id"] == self.claim_message.id
                    and not entry["claimed"]):
                    slot_entry = entry
                    slot_key = key
                    break

            if not slot_entry:
                return await interaction.followup.send(
                    "This slot has already been claimed by someone else.", ephemeral=True
                )

            # enforce 2 lobbies max per day: team at the limit can't claim more
            if self.is_t3:
                if (self.team_name, self.group) in constants.special_registered_set:
                    return await interaction.followup.send(
                        "Your team is already registered in this group.", ephemeral=True
                    )
                if sum(1 for entry in constants.special_registered_set if entry[0] == self.team_name) >= constants.MAX_T3_GROUP_REGISTRATIONS:
                    return await interaction.followup.send(
                        "Your team is already registered in 2 lobbies (2 lobbies max per day).", ephemeral=True
                    )
            else:
                if any(entry[0] == self.team_name for entry in constants.registered_set):
                    return await interaction.followup.send(
                        "Your team is already registered in another lobby (1 lobby max per day).", ephemeral=True
                    )

            # add team to the lobby JSON file
            json_file_name = f"alt_lobby_{self.lobby_number}_teams.json" if self.is_t3 else f"lobby_{self.lobby_number}_teams.json"
            try:
                with open(json_file_name, 'r') as f:
                    lobby_data = json.load(f)
            except FileNotFoundError:
                return await interaction.followup.send("Lobby data not found.", ephemeral=True)

            # replace the CANCELLED placeholder with the claiming team at the same position
            cancel_key = slot_entry.get("cancel_key")
            if cancel_key and cancel_key in lobby_data:
                items = list(lobby_data.items())
                position = next(i for i, (k, v) in enumerate(items) if k == cancel_key)
                items[position] = (self.team_name, interaction.user.id)
                lobby_data = dict(items)
            else:
                # fallback: just append if cancel_key not found
                lobby_data[self.team_name] = interaction.user.id

            with open(json_file_name, 'w') as f:
                json.dump(lobby_data, f, indent=1)

            # assign IDP role
            if self.is_t3:
                role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"T3 G{self.lobby_number} IDP")
            else:
                role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"Group {self.lobby_number} IDP")

            if role:
                try:
                    await interaction.user.add_roles(role)
                except Exception as e:
                    print(f"Error assigning IDP role: {e}")

            # update the slot list embed in the IDP channel
            await update_slot_list_after_cancel(self.lobby_number, self.is_t3)

            # mark as claimed in cancelled_slots.json
            cancelled_data[slot_key]["claimed"] = True
            cancelled_data[slot_key]["claimed_by"] = self.team_name
            save_cancelled_slots(cancelled_data)
            try:
                cancel_claim_expiry(slot_key)
            except Exception:
                pass
            try:
                state_snapshot.save_state_snapshot()
            except Exception as e:
                print(f"[snapshot] save failed (non-fatal): {e}")

            # delete the claim message since slot is now filled
            try:
                await self.claim_message.delete()
            except Exception as e:
                print(f"Error deleting claim message: {e}")

        await interaction.followup.send(
            f'Slot claimed! You are now in Group {self.group}, Lobby {self.lobby_number}.\n'
            f'Check your IDP channel for the slot list.',
            ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await interaction.response.send_message(
            "Something went wrong during claiming. Please try again.",
            ephemeral=True, delete_after=30
        )
        print(f"Error in ClaimConfirmModal: {error}")

# ──────────────────────────────────────────────────────────────────

class PlayerSelectView(discord.ui.View):
    def __init__(self,row,role):
        super().__init__(timeout=None)
        self.add_item(PlayerSelectDropdown(row,role))

class ModToolsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TeamInfoButton())
        self.add_item(AddTeamButton())
        self.add_item(CopyTeamNamesButton())

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
        await interaction.response.send_message(
            "An error occurred while processing your request. Please try again later.",
            ephemeral=True, delete_after=240
        )
        print(f"An error occurred during the team name change request for {interaction.user}: {error}")

async def validate_captcha(captcha_phrase : str, sum1_answer : int, sum2_answer : int):
    # Placeholder for actual captcha validation logic
    return (captcha_phrase.lower().rstrip() == constants.captcha_question_variables[0] and sum1_answer == int(constants.captcha_question_variables[1] + constants.captcha_question_variables[2]) and sum2_answer == int(constants.captcha_question_variables[3] + constants.captcha_question_variables[4]))  # Replace with actual validation
    
# Event handler for when the bot is ready
@bot.event
async def on_ready():

    print(f"We have logged in as {bot.user} but wait we ain't ready")

    try:
        state_snapshot.load_state_snapshot()
    except Exception as e:
        print(f"[snapshot] restore in on_ready failed (non-fatal): {e}")

    await bot.tree.sync()  # For both text and slash commands

    if constants.ENROLLMENT_MESSAGE_ID and bot.get_channel(constants.ENROLLMENT_CHANNEL_ID):
        try:
            # Fetch the message
            message = await bot.get_channel(constants.ENROLLMENT_CHANNEL_ID).fetch_message(constants.ENROLLMENT_MESSAGE_ID)
        except discord.NotFound:
            # If the message is not found, handle the case gracefully
            message = None

        if message:
            # Edit the existing message with the dropdown menu
            await message.edit(view=TournamentView())
        else:
            # Send a new message with the dropdown menu
            message = await send_selectmenu(bot.get_channel(constants.ENROLLMENT_CHANNEL_ID))
        
        # Update the interaction message ID
        constants.ENROLLMENT_MESSAGE_ID = message.id

    else:
        # If the channel or message ID is not valid, send the select menu
        message = await send_selectmenu(bot.get_channel(constants.ENROLLMENT_CHANNEL_ID))
        constants.ENROLLMENT_MESSAGE_ID = message.id

    #     # Handle Tournament View persistence
    # if constants.PREFERENCE_MESSAGE_ID and bot.get_channel(constants.PREF_SELECTION_CHANNEL_ID):
    #     try:
    #         # Fetch the message
    #         message = await bot.get_channel(constants.PREF_SELECTION_CHANNEL_ID).fetch_message(constants.PREFERENCE_MESSAGE_ID)
    #     except discord.NotFound:
    #         # If the message is not found, reset the interaction message ID
    #         constants.PREFERENCE_MESSAGE_ID = None
    #         return
    #     if message:
    #         await message.edit(view=LobbyPreferencesView())
    #     else:
    #         message = await send_pref_menu(bot.get_channel(constants.PREF_SELECTION_CHANNEL_ID))
    #         constants.PREFERENCE_MESSAGE_ID = message.id
    # else:
    #     # If the channel or message ID is not valid, send the select menu
    #     await send_pref_menu(bot.get_channel(constants.PREF_SELECTION_CHANNEL_ID))

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

    # if constants.SCRIMS_INFO_MESSAGE_ID and bot.get_channel(constants.INFO_CHANNEL_ID):
    #     try:
    #         # Fetch the message
    #         message = await bot.get_channel(constants.INFO_CHANNEL_ID).fetch_message(constants.SCRIMS_INFO_MESSAGE_ID)
    #     except discord.NotFound:
    #         # If the message is not found, handle the case gracefully
    #         message = None

    #     if message:
    #         # Edit the existing message with the dropdown menu
    #         await message.edit(view=ScrimsOverviewView())
    #     else:
    #         # Send a new message with the dropdown menu
    #         message = await send_overview_menu(bot.get_channel(constants.INFO_CHANNEL_ID))
        
    #     # Update the interaction message ID
    #     constants.SCRIMS_INFO_MESSAGE_ID = message.id

    # else:
    #     # If the channel or message ID is not valid, send the select menu
    #     message = await send_overview_menu(bot.get_channel(constants.INFO_CHANNEL_ID))
    #     constants.SCRIMS_INFO_MESSAGE_ID = message.id

    # if constants.SCRIMS_INFO_MESSAGE_ID and bot.get_channel(constants.INFO_CHANNEL_ID):
    #     try:
    #         # Fetch the message
    #         message = await bot.get_channel(constants.INFO_CHANNEL_ID).fetch_message(constants.SCRIMS_INFO_MESSAGE_ID)
    #     except discord.NotFound:
    #         # If the message is not found, handle the case gracefully
    #         message = None

    #     if message:
    #         # Edit the existing message with the dropdown menu
    #         await message.edit(view=ScrimsOverviewView())
    #     else:
    #         # Send a new message with the dropdown menu
    #         message = await send_overview_menu(bot.get_channel(constants.INFO_CHANNEL_ID))
        
    #     # Update the interaction message ID
    #     constants.SCRIMS_INFO_MESSAGE_ID = message.id

    # else:
    #     # If the channel or message ID is not valid, send the select menu
    #     message = await send_overview_menu(bot.get_channel(constants.INFO_CHANNEL_ID))
    #     constants.SCRIMS_INFO_MESSAGE_ID = message.id

    # if constants.FAQ_MESSAGE_ID and bot.get_channel(constants.HOW_TO_PLAY_CHANNEL_ID):
    #     try:
    #         # Fetch the message
    #         message = await bot.get_channel(constants.HOW_TO_PLAY_CHANNEL_ID).fetch_message(constants.FAQ_MESSAGE_ID)
    #     except discord.NotFound:
    #         # If the message is not found, handle the case gracefully
    #         message = None

    #     if message:
    #         # Edit the existing message with the dropdown menu
    #         await message.edit(view=FaqView())

    #     # Update the interaction message ID
    #     constants.FAQ_MESSAGE_ID = message.id

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

    start_auto.start()
    clear_lb_auto.start()
    auto_close_reg.start()
    # idploop.start()
    # idploop2.start()
    # idploop3.start()
    # idploop4.start()
    # idploop5.start()
    # idploop6.start()
    t3rulesreminder.start()
    t3rulesreminder2.start()
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
    
    print(f"Set bro.")

@bot.event
async def on_guild_join(guild):
    # Sync commands for the new guild
    await bot.tree.sync(guild=guild)
    print(f"Slash commands synced in new guild: {guild.name}")
    
async def connect_to_google_sheets(json_keyfile_path, sheet_id,retry_interval=1):
    while True:
        try:
            credentials = Credentials.from_service_account_file(json_keyfile_path, scopes=['https://www.googleapis.com/auth/spreadsheets'])
            gc = gspread.authorize(credentials)
            print("Successfully authenticated with Google Sheets.")

            service = build('sheets', 'v4', credentials=credentials)

            sheet = gc.open_by_key(sheet_id).sheet1
            print("Successfully opened Google Sheets document by ID:", sheet_id)

            return sheet, service

        except Exception as e:
            print("Error while connecting to Google Sheets:", e)
            print(f"Retrying in {retry_interval} seconds...")
            await asyncio.sleep(retry_interval)

# @bot.hybrid_command(name="setprompt")
# @app_commands.describe(prompt="karle bhai prompt set koi ni dekhra")
# @commands.has_permissions(view_audit_log=True, manage_roles=True)
# async def setprompt(ctx, *, prompt: str = None):
#     try:
#         if prompt is None:
#             await ctx.send("Invalid prompt. Please provide a non-empty prompt.")
#             return

#         # Check if the prompt is not an empty string
#         if prompt.strip():
#             constants.REGISTRATION_PROMPT = prompt
#             await ctx.send(f"Registration prompt set to: {prompt}")
#         else:
#             await ctx.send("Invalid prompt. Please provide a non-empty prompt.")

#     except MissingPermissions as e:
#         await ctx.send(f"You don't have the required permissions to use this command: {', '.join(e.missing_perms)}")
#     except Exception as e:
#         await ctx.send(f"An error occurred: {e}")

# @setprompt.error
# async def setprompt(ctx, error):
#     if isinstance(error, commands.MissingPermissions):
#         missing_perms = ', '.join(error.missing_permissions)
#         await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
#     else:
#         await ctx.send(f"An error occurred: {error}")

# @bot.hybrid_command(name="start")
# @commands.has_permissions(view_audit_log=True, manage_roles=True)
# async def start(ctx):
#     constants.registered_teams.clear()
#     try:
#         os.remove('registered_teams.csv')
#         print("CSV file deleted successfully.")

#     except FileNotFoundError:
#         await bot.get_channel(constants.MOD_CHANNEL_ID).send("Error: CSV file not found.")

#     await unlock_channel(constants.REGISTRATION_CHANNEL_ID)
#     await ctx.send("## STARTED")

# @start.error
# async def start_error(ctx, error):
#     if isinstance(error, commands.MissingPermissions):
#         missing_perms = ', '.join(error.missing_permissions)
#         await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
#     else:
#         await ctx.send(f"An error occurred: {error}")

async def start_registration(captcha_phrase: str):
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

        with open('lobby_details.json', 'w') as json_file:
            json.dump({}, json_file)

        with open('lobby_details2.json', 'w') as json_file:
            json.dump({}, json_file)

    except discord.HTTPException as e:
        print(f"An error occurred while purging messages: {e}")

    constants.captcha_question_variables.extend([
        captcha_phrase.lower().rstrip(),
        random.randint(10, 99),
        random.randint(10, 99),
        random.randint(10, 99),
        random.randint(10, 99),
    ])
    try:
        state_snapshot.save_state_snapshot()
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

    if target == "main":
        await break_main_registration()
    else:
        await break_special_registration()

    await ctx.send(f"Broke registration: {target}")

async def break_main_registration():
    constants.disabled_status = True
    message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
    await message.edit(view=RegistrationView2())
    await save_as_csv(constants.registered_teams, 'registered_teams.csv', save_all_flag=True)
    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File('registered_teams.csv'))
    for lobby_number, lobby_teams_dict in enumerate(constants.lobby_teams, 1):
        json_file_name = f"lobby_{lobby_number}_teams.json"
        with open(json_file_name, 'w') as f:
            json.dump(lobby_teams_dict, f, indent=1)
        team_names = list(lobby_teams_dict.keys())
        async with asyncio.TaskGroup() as taskhandler:
            taskhandler.create_task(
                bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File(json_file_name))
            )
            try:
                idp_channel = discord.utils.get(
                    bot.get_guild(constants.GUILD_ID).channels,
                    name=f"group-{lobby_number}-idp"
                )
                await send_slots_list(team_names, lobby_number, idp_channel, edit_slots_list=True, cancel_disabled=False)
            except Exception as e:
                print(f"Got Exception when sending lobby csv files: {e}")
    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(
        f"You can download the Google Sheets app to view the list of users and their registration timestamps of {datetime.datetime.today().strftime('%d %b')} from this CSV file (for transparency). If you cant find you name in these, you were later than all these 😢.",
        file=discord.File('timestamps.csv')
    )
    with open('lobby_details.json', 'w') as json_file:
        json.dump(constants.temp_json_dict, json_file, indent=1)
    try:
        await refresh_reg_message(is_t3=False, show_slots=False, force=True)
    except Exception as e:
        print('[reg-slots] hide failed (non-fatal):', e)


async def break_special_registration():
    try:
        constants.special_disabled_status = True
        special_message = await bot.get_channel(constants.SPECIAL_REGISTRATION_CHANNEL_ID).fetch_message(constants.SPECIAL_REG_MESSAGE_ID)
        await special_message.edit(view=RegistrationView3())
        if constants.special_registered_teams:
            await save_as_csv(constants.special_registered_teams, 'special_registered_teams.csv', save_all_flag=True)
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File('special_registered_teams.csv'))
            for lobby_number, lobby_teams_dict in enumerate(constants.special_lobby_teams, 1):
                json_file_name = f"alt_lobby_{lobby_number}_teams.json"
                with open(json_file_name, 'w') as f:
                    json.dump(lobby_teams_dict, f, indent=1)
                team_names = list(lobby_teams_dict.keys())
                async with asyncio.TaskGroup() as taskhandler:
                    taskhandler.create_task(
                        bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File(json_file_name))
                    )
                    try:
                        idp_channel = discord.utils.get(
                            bot.get_guild(constants.GUILD_ID).channels,
                            name=f"t3-idp-{lobby_number}"
                        )
                        await send_slots_list(team_names, lobby_number, idp_channel, use_alt_lobby=True, edit_slots_list=True, cancel_disabled=False)
                    except Exception as e:
                        print(f"Got Exception when sending special lobby files: {e}")
            with open('lobby_details2.json', 'w') as json_file:
                json.dump(constants.temp_json_dict2, json_file, indent=1)
            try:
                await refresh_reg_message(is_t3=True, show_slots=False, force=True)
            except Exception as e:
                print('[reg-slots] hide failed (non-fatal):', e)
    except Exception as e:
        print(f"Error breaking special registration: {e}")


@break_reg.error
async def break_reg_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")

# @bot.hybrid_command(name="editslots", description="Change the slots limit and lobby size.")
# @commands.has_permissions(view_audit_log=True, manage_roles=True)
# async def editslots(ctx, slots_limit: int, lobby_size: int):
#     await ctx.defer()

#     constants.SLOTS_LIMIT = slots_limit
#     constants.LOBBY_SIZE = lobby_size
#     constants.lobby_teams = [{} for _ in range(int(slots_limit / lobby_size))]

#     try:
#         message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
#         await message.edit(view=RegistrationView2())

#     except discord.HTTPException as e:
#         print(f"An error occurred while purging messages: {e}")

#     await ctx.send(f"Slots limit and lobby size updated:\nSlots limit: {slots_limit}\nLobby size: {lobby_size}")

# @editslots.error
# async def editslots_error(ctx, error):
#     if isinstance(error, commands.MissingPermissions):
#         missing_perms = ', '.join(error.missing_permissions)
#         await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
#     else:
#         await ctx.send(f"An error occurred: {error}")

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
    row = [team_name,int(time.time()),int((hours * 3600) + (days * 86400)),datetime.datetime.now(tz=constants.timezone).strftime("%Y-%m-%d %H:%M"),f"{days} days {hours} hours",str(user)]
    constants.ban_sheet.append_row(row)
    await interaction.response.send_message(f"Banned User: {user.mention}'s Team {team_name[5:] if team_name.lower().startswith('team ') else team_name} for {days} days and {hours} hours")
    # Print registration details for verification
    print(f"Banned {team_name} for {days} days and {hours} hours")

@ban_team.error
async def ban_team_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await interaction.response.send_message(f"You don't have the required permissions to use this command: {missing_perms}")
    elif isinstance(error, ValueError):
        await interaction.response.send_message("You must specify a valid duration.")
    else:
        await interaction.response.send_message(f"An error occurred: {error}")

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
        row = [str(user.id),int(time.time()),int((hours * 3600) + (days * 86400)),datetime.datetime.now(tz=constants.timezone).strftime("%Y-%m-%d %H:%M"),f"{days} days {hours} hours",reason]
        constants.blacklist_sheet.append_row(row)
        await interaction.response.send_message(f"Blacklisted User: {user.mention} for {days} days and {hours} hours")
        print(f"Blacklisted User: {user.mention}'s for {days} days and {hours} hours")

@blacklist_user.error
async def blacklist_user_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await interaction.response.send_message(f"You don't have the required permissions to use this command: {missing_perms}")
    elif isinstance(error, ValueError):
        await interaction.response.send_message("You must specify a valid duration.")
    else:
        await interaction.response.send_message(f"An error occurred: {error}")

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

# @bot.hybrid_command(name="clearcd", description="**Clear Cool-down role")
# @commands.has_permissions(view_audit_log=True, manage_roles=True)
# async def clearcd(ctx):

#     await ctx.send("kr rha thoda wait krna ..")

#     try:
#         role = bot.get_guild(constants.GUILD_ID).get_role(constants.COOLDOWN_ROLE_ID)
#         if role:
#             for member in role.members:
#                 await member.remove_roles(role)

#         await ctx.send("Cooldown role is cleared now.")
    
#     except discord.Forbidden:
#         await ctx.send("I do not have permission to manage roles or channels.")
#     except discord.HTTPException as e:
#         await ctx.send(f"An HTTP error occurred: {e}")    
#     except Exception as e:
#         await ctx.send(f"An error occurred: {e}")

# @clearcd.error
# async def clearcd(ctx, error):
#     if isinstance(error, commands.MissingPermissions):
#         missing_perms = ', '.join(error.missing_permissions)
#         await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
#     else:
#         await ctx.send(f"An error occurred: {error}")

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
        
# @bot.hybrid_command(name="spamloop", description="spam loop")
# @commands.has_permissions(manage_roles=True,view_audit_log=True)
# async def spamloop(ctx: commands.Context,*, message: str,time : int):
#     try:
#         for _ in range(0,99999999999999):
#             await ctx.channel.send(message)
#             await asyncio.sleep(time)
            
#     except discord.HTTPException as e:
#         await ctx.send(f"An error occurred while sending the message: {e}")

# @spamloop.error
# async def spamloop(ctx: commands.Context, error: commands.CommandError):
#     if isinstance(error, commands.MissingPermissions):
#         await ctx.send("You don't have the required permissions to use this command.")
#     elif isinstance(error, commands.ChannelNotFound):
#         await ctx.send("The specified channel was not found.")
#     else:
#         await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="inrole", description="inrole users and team names")
@commands.has_any_role(*constants.roles_for_bot_access)
async def inrole(ctx: commands.Context, role: discord.Role):
    try:
        # Create a list of formatted mentions asynchronously
        mentions_list = [
            f"{member.mention} : {await validate_registration(member, check_cooldown=False, check_left_server=False)}"
            for member in role.members
        ]
        # Join the list into a single string separated by newlines
        mentions = '\n'.join(mentions_list)
        await ctx.send(mentions)
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

@bot.hybrid_command(name="add_team", description="Add team in T3 Slots List.")
@commands.has_any_role(*constants.roles_for_bot_access)
async def add_team(ctx: commands.Context, team_name: str,member: discord.Member,channel: discord.TextChannel):
    try:
        await add_team_slotlist(team_name,member,channel)
        await ctx.send(f"{team_name} added in {channel.mention} by {ctx.author.name}")
    except discord.HTTPException as e:
        await ctx.send(f"Error aaya: {e}")

@add_team.error
async def add_team_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")

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

        if len(teams) != 16:
            await interaction.channel.send("Error: The results string must contain exactly 16 teams separated by '<>'.")
            return
        
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

local_tz = datetime.datetime.now().astimezone().tzinfo
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

async def clear_lobbies(purge_all=False, before_time=None):
    guild = bot.get_guild(constants.GUILD_ID)

    lobby_role_names = [f"Group {i} IDP" for i in range(1, int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE) + 1)]
    lobby_channel_names = [f"group-{i}-idp" for i in range(1, int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE) + 1)]

    # Add T3
    lobby_role_names.extend(
        [f"T3 G{i} IDP" for i in range(1, int(constants.SPECIAL_SLOTS_LIMIT / constants.SPECIAL_LOBBY_SIZE) + 1)]
    )

    lobby_channel_names.extend(
        [f"t3-idp-{i}" for i in range(1, int(constants.SPECIAL_SLOTS_LIMIT / constants.SPECIAL_LOBBY_SIZE) + 1)]
    )

    for role_name in lobby_role_names:
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            for member in role.members:
                await member.remove_roles(role)

    for channel_name in lobby_channel_names:
        channel = discord.utils.get(guild.channels, name=channel_name)
        if channel:
            if purge_all:
                await channel.purge()
            elif before_time:
                await channel.purge(after=(datetime.datetime.now() - datetime.timedelta(hours=24)), before=before_time)
            else:
                await channel.purge(after=(datetime.datetime.now() - datetime.timedelta(hours=24)))

@bot.hybrid_command(name="clearlb", description="**Clear lobby Channels and role")
@commands.has_any_role(*constants.roles_for_bot_access)
async def clear_lb(ctx):

    await ctx.send("kr rha thoda wait krna ..")

    try:
        await clear_lobbies(before_time=ctx.message.created_at)
        await ctx.send("Lobby channels (last 24 hrs) and roles are cleared now.")
    
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
                if now >= earliest - datetime.timedelta(minutes=30):
                    if is_t3:
                        if not constants.special_disabled_status:
                            print('[auto-close] closing T3 reg 30 min before M1')
                            await break_special_registration()
                    else:
                        if not constants.disabled_status:
                            print('[auto-close] closing open reg 30 min before M1')
                            await break_main_registration()
            except Exception as e:
                print('[auto-close] failed (non-fatal):', e)
    except Exception as e:
        print('[auto-close] loop failed (non-fatal):', e)


idt1 = datetime.time(hour=15, minute=56, tzinfo=local_tz)
@tasks.loop(time=idt1)
async def idploop():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        try:
            # Start the inner loop to run every 10 minutes after the initial execution
            inner_loop1.start()
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"hey")

        except Exception as e:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
            print(f"Exception aayi: {e}")

@tasks.loop(minutes=10)
async def inner_loop1():
    
    try:
        constants.inner_loop_counter += 1

        lobby_number = int(int(constants.inner_loop_counter) % ((int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE))/2))
        if lobby_number == 0:
            lobby_number = int((int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE))/2)

        print(lobby_number)
        role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name= f"Group {lobby_number} IDP")
        idchannel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"group-{lobby_number}-idp")

        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Please enter Match {lobby_number} ID.")
        
        def check(msg):
            if msg.channel.id == constants.UPDATES_CHANNEL_ID and msg.content.isdigit() and msg.author.id != bot.user.id:
                member = bot.get_guild(constants.GUILD_ID).get_member(msg.author.id)
                if member and any(role.name in constants.roles_for_purge_perm for role in member.roles):
                    return True
            return False
        
        response = await bot.wait_for(
    'message',
    check=check,
    timeout=390
)
        
        if response.content.strip():  # Check if the response is not empty after stripping whitespace
            # Process the response if needed
            current_time = datetime.datetime.now(local_tz)
            match_times = constants.match_schedule[lobby_number][1]
            start_time_str = match_times['st']
            id_time_str = match_times['idt']
            start_time = datetime.datetime.strptime(start_time_str, "%I:%M %p").replace(tzinfo=local_tz)
            id_time = datetime.datetime.strptime(id_time_str, "%I:%M %p").replace(tzinfo=local_tz)
            if current_time.time() >= id_time.time():
                final_start_time = (current_time + datetime.timedelta(minutes=5)).time()
            else:
                final_start_time = start_time
            await idchannel.send(f"""TRIDENT ESPORTS TIER 3 SCRIMS 
{role.mention}

MATCH - 01
MAP- ERANGLE

ID - {response.content}
PASS - TG{lobby_number}{lobby_number}{lobby_number}
START - {final_start_time.strftime('%I:%M %p')}
 
TEAMS ARE REQUESTED TO JOIN 2 MINS PRIOR TO THE START TIME

Rules Strictly To Be Followed:-

1. Sit according to the allotted slots!
2. Pov recording is mandatory for all players. We may request 'Raw Pov' at any time.
3. Emergency pickups are strictly prohibited.
4. Missing a single match will result in a team ban.""")
            
        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"IDP SENT BORO, START TIME SHALL BE {final_start_time.strftime('%I:%M %p')} {response.author.mention}")

    except asyncio.TimeoutError:
        pass

    except Exception as e:
        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
        print(f"Exception aayi: {e}")

idt2 = datetime.time(hour=16, minute=34, tzinfo=local_tz)
@tasks.loop(time=idt2)
async def idploop2():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        try:
            inner_loop1.cancel()
            await asyncio.sleep(120)
            constants.inner_loop_counter = 0
            # Start the inner loop to run every 10 minutes after the initial execution
            inner_loop2.start()
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"hey")

        except Exception as e:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
            print(f"Exception aayi: {e}")

@tasks.loop(minutes=10)
async def inner_loop2():
    try:
        constants.inner_loop_counter += 1

        lobby_number = int(int(constants.inner_loop_counter) % ((int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE))/2))
        if lobby_number == 0:
            lobby_number = int((int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE))/2)

        print(lobby_number)
        role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name= f"Group {lobby_number} IDP")
        idchannel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"group-{lobby_number}-idp")

        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Please enter Match {lobby_number} ID.")
        
        def check(msg):
            if msg.channel.id == constants.UPDATES_CHANNEL_ID and msg.content.isdigit() and msg.author.id != bot.user.id:
                member = bot.get_guild(constants.GUILD_ID).get_member(msg.author.id)
                if member and any(role.name in constants.roles_for_purge_perm for role in member.roles):
                    return True
            return False
        
        response = await bot.wait_for(
    'message',
    check=check,
    timeout=390
)
        
        if response.content.strip():  # Check if the response is not empty after stripping whitespace
            # Process the response if needed
            current_time = datetime.datetime.now(local_tz)
            match_times = constants.match_schedule[lobby_number][2]
            start_time_str = match_times['st']
            id_time_str = match_times['idt']
            start_time = datetime.datetime.strptime(start_time_str, "%I:%M %p").replace(tzinfo=local_tz)
            id_time = datetime.datetime.strptime(id_time_str, "%I:%M %p").replace(tzinfo=local_tz)
            if current_time.time() >= id_time.time():
                final_start_time = (current_time + datetime.timedelta(minutes=6)).time()
            else:
                final_start_time = start_time
            await idchannel.send(f"""TRIDENT ESPORTS TIER 3 SCRIMS 
{role.mention}
                                 
MATCH - 02
MAP- RONDO

ID - {response.content}
PASS - TG{lobby_number}{lobby_number}{lobby_number}
START - {final_start_time.strftime('%I:%M %p')}
 
TEAMS ARE REQUESTED TO JOIN 2 MINS PRIOR TO THE START TIME

Rules Strictly To Be Followed:-

1. Sit according to the allotted slots!
2. Pov recording is mandatory for all players. We may request 'Raw Pov' at any time.
3. Emergency pickups are strictly prohibited.
4. Missing a single match will result in a team ban.""")
            
        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"IDP SENT BORO, START TIME SHALL BE {final_start_time.strftime('%I:%M %p')} {response.author.mention}")

    except asyncio.TimeoutError:
        pass
    
    except Exception as e:
        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
        print(f"Exception aayi: {e}")

idtloopstop = datetime.time(hour=17, minute=14, tzinfo=local_tz)
@tasks.loop(time=idtloopstop)
async def idploop3():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        try:
            inner_loop2.cancel()
            constants.inner_loop_counter = 0
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"done for the day")

        except Exception as e:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
            print(f"Exception aayi: {e}")

idt3 = datetime.time(hour=17, minute=56, tzinfo=local_tz)
@tasks.loop(time=idt3)
async def idploop4():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        try:
            # Start the inner loop to run every 10 minutes after the initial execution
            inner_loop3.start()
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"hey")

        except Exception as e:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
            print(f"Exception aayi: {e}")

@tasks.loop(minutes=10)
async def inner_loop3():
    
    try:
        constants.inner_loop_counter += 1

        _half = int((int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)) // 2)
        _rem = int(constants.inner_loop_counter) % _half
        if _rem == 0:
            _rem = _half
        lobby_number = int(_rem + _half)

        print(lobby_number)
        role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name= f"Group {lobby_number} IDP")
        idchannel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"group-{lobby_number}-idp")

        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Please enter Match {lobby_number} ID.")
        
        def check(msg):
            if msg.channel.id == constants.UPDATES_CHANNEL_ID and msg.content.isdigit() and msg.author.id != bot.user.id:
                member = bot.get_guild(constants.GUILD_ID).get_member(msg.author.id)
                if member and any(role.name in constants.roles_for_purge_perm for role in member.roles):
                    return True
            return False
        
        response = await bot.wait_for(
    'message',
    check=check,
    timeout=390
)
        
        if response.content.strip():  # Check if the response is not empty after stripping whitespace
            # Process the response if needed
            current_time = datetime.datetime.now(local_tz)
            match_times = constants.match_schedule[lobby_number][1]
            start_time_str = match_times['st']
            id_time_str = match_times['idt']
            start_time = datetime.datetime.strptime(start_time_str, "%I:%M %p").replace(tzinfo=local_tz)
            id_time = datetime.datetime.strptime(id_time_str, "%I:%M %p").replace(tzinfo=local_tz)
            if current_time.time() >= id_time.time():
                final_start_time = (current_time + datetime.timedelta(minutes=5)).time()
            else:
                final_start_time = start_time
            await idchannel.send(f"""TRIDENT ESPORTS TIER 3 SCRIMS 
{role.mention}

MATCH - 01
MAP- ERANGLE

ID - {response.content}
PASS - TG{lobby_number}{lobby_number}{lobby_number}
START - {final_start_time.strftime('%I:%M %p')}
 
TEAMS ARE REQUESTED TO JOIN 2 MINS PRIOR TO THE START TIME

Rules Strictly To Be Followed:-

1. Sit according to the allotted slots!
2. Pov recording is mandatory for all players. We may request 'Raw Pov' at any time.
3. Emergency pickups are strictly prohibited.
4. Missing a single match will result in a team ban.""")
            
        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"IDP SENT BORO, START TIME SHALL BE {final_start_time.strftime('%I:%M %p')} {response.author.mention}")

    except asyncio.TimeoutError:
        pass

    except Exception as e:
        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
        print(f"Exception aayi: {e}")

idt4 = datetime.time(hour=18, minute=34, tzinfo=local_tz)
@tasks.loop(time=idt4)
async def idploop5():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        try:
            inner_loop3.cancel()
            await asyncio.sleep(120)
            constants.inner_loop_counter = 0
            # Start the inner loop to run every 10 minutes after the initial execution
            inner_loop4.start()
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"hey")

        except Exception as e:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
            print(f"Exception aayi: {e}")

@tasks.loop(minutes=10)
async def inner_loop4():
    try:
        constants.inner_loop_counter += 1

        _half = int((int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)) // 2)
        _rem = int(constants.inner_loop_counter) % _half
        if _rem == 0:
            _rem = _half
        lobby_number = int(_rem + _half)

        print(lobby_number)
        role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name= f"Group {lobby_number} IDP")
        idchannel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"group-{lobby_number}-idp")

        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Please enter Match {lobby_number} ID.")
        
        def check(msg):
            if msg.channel.id == constants.UPDATES_CHANNEL_ID and msg.content.isdigit() and msg.author.id != bot.user.id:
                member = bot.get_guild(constants.GUILD_ID).get_member(msg.author.id)
                if member and any(role.name in constants.roles_for_purge_perm for role in member.roles):
                    return True
            return False
        
        response = await bot.wait_for(
    'message',
    check=check,
    timeout=390
)
        
        if response.content.strip():  # Check if the response is not empty after stripping whitespace
            # Process the response if needed
            current_time = datetime.datetime.now(local_tz)
            match_times = constants.match_schedule[lobby_number][2]
            start_time_str = match_times['st']
            id_time_str = match_times['idt']
            start_time = datetime.datetime.strptime(start_time_str, "%I:%M %p").replace(tzinfo=local_tz)
            id_time = datetime.datetime.strptime(id_time_str, "%I:%M %p").replace(tzinfo=local_tz)
            if current_time.time() >= id_time.time():
                final_start_time = (current_time + datetime.timedelta(minutes=6)).time()
            else:
                final_start_time = start_time
            await idchannel.send(f"""TRIDENT ESPORTS TIER 3 SCRIMS 
{role.mention}
                                 
MATCH - 02
MAP- RONDO

ID - {response.content}
PASS - TG{lobby_number}{lobby_number}{lobby_number}
START - {final_start_time.strftime('%I:%M %p')}
 
TEAMS ARE REQUESTED TO JOIN 2 MINS PRIOR TO THE START TIME

Rules Strictly To Be Followed:-

1. Sit according to the allotted slots!
2. Pov recording is mandatory for all players. We may request 'Raw Pov' at any time.
3. Emergency pickups are strictly prohibited.
4. Missing a single match will result in a team ban.""")
            
        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"IDP SENT BORO, START TIME SHALL BE {final_start_time.strftime('%I:%M %p')} {response.author.mention}")

    except asyncio.TimeoutError:
        pass
    
    except Exception as e:
        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
        print(f"Exception aayi: {e}")

idtloopstop2 = datetime.time(hour=19, minute=14, tzinfo=local_tz)
@tasks.loop(time=idtloopstop2)
async def idploop6():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        try:
            inner_loop4.cancel()
            constants.inner_loop_counter = 0
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"done for the day")

        except Exception as e:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
            print(f"Exception aayi: {e}")

rulesremindertime = datetime.time(hour=13, minute=00, tzinfo=local_tz)
@tasks.loop(time=rulesremindertime)
async def t3rulesreminder():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        try:
            lobby_channel_names = [f"group-{i}-idp" for i in range(1, int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE) + 1)]

            for channel_name in lobby_channel_names:
                channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=channel_name)
                if channel:
                    await channel.send("Reminder to read rules written in slots list and rules channel.")

        except Exception as e:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
            print(f"Exception aayi: {e}")

rulesremindertime2 = datetime.time(hour=13, minute=50, tzinfo=local_tz)
@tasks.loop(time=rulesremindertime2)
async def t3rulesreminder2():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        try:
            lobby_channel_names = [f"group-{i}-idp" for i in range(1, int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE) + 1)]

            for channel_name in lobby_channel_names:
                channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=channel_name)
                if channel:
                    pov_message = """Hello Teams,

Please follow these steps to record your Point of View (POV) while playing BGMI:
1. Before opening the BGMI app show the list of background running apps on your device
2. Go to the PlayStore (for Android users) or Appstore (for iOS users) and open the BGMI app.
3. Join the Lobby using the provided details.
4. Before starting the match, ensure that both your in-game audio and your own voice (microphone) are being recorded.
5. Play the match.
6. After each match, make sure to show the list of background running apps and IMEI on your device.
7. You need to repeat the above steps for every match you play."""
                    await channel.send(pov_message)

        except Exception as e:
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Exception aayi: {e}")
            print(f"Exception aayi: {e}")

# @bot.event
# async def on_message(message):
#     # Check if the message is in the desired channel
#     if message.channel.id == constants.REGISTRATION_CHANNEL_ID:
#         if message.content.strip() == constants.REGISTRATION_PROMPT:
#             await confirm(message.author.id,message.id)

#         elif message.author.bot:
#             return
        
#         else:
#             await bot.process_commands(message)
        
#     else:
#         # Process bot commands if the message doesn't match the registration prompt
#         await bot.process_commands(message)

# async def confirm(user_id, message_id):

#     channel = bot.get_channel(constants.REGISTRATION_CHANNEL_ID) 
#     if channel:
#         try:
#             message = await channel.fetch_message(message_id)  # Fetch message from the channel
#         except discord.NotFound:
#             print("Error: Message not found.")
#     else:
#         print("Error: Channel not found.")

#     # Check if the user is already enrolled
#     team_name = await validate_registration(user_id)
#     if team_name:
#         if team_name == 'banned':
#             # Send a message to the user with the reason for the ban
#             user = bot.get_user(user_id)
#             if user:
#                 await bot.get_channel(constants.SCRIMS_LOG_CHANNEL_ID).send(f"{user.mention} Someone from your team is banned at the moment.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.")
#             else:
#                 print("Error: User not found.")
#             await message.add_reaction('❌')

#         elif team_name == 'cooldown':
#             # Send a message to the user informing about the cooldown
#             user = bot.get_user(user_id)
#             if user:
#                 await bot.get_channel(constants.SCRIMS_LOG_CHANNEL_ID).send(f"{user.mention} Someone from your team is on cooldown, please wait for the cooldown period to end\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.")
#             else:
#                 print("Error: User not found.")
#             await message.add_reaction('❌')

#         elif user_id not in constants.registered_teams:  # Check if the user is not already registered
#             if available_slots() > 0:
#                 print("Available slots:", available_slots())
#                 await message.add_reaction('✅')
#                 # Mark registration as confirmed
#                 await confirm_registration(user_id, team_name)  # Pass team name
#                 print("Registration confirmed for user:", user_id)
#                 # Save the registered team's data
#                 constants.registered_teams[user_id] = team_name
#                 print("Registered teams' data:", constants.registered_teams)  # Log the registered teams' data
#                 print("Available slots:", available_slots())
                
#                 # Assign COOLDOWN_ROLE_ID to the confirmed user
#                 await assign_role(user_id, constants.COOLDOWN_ROLE_ID)
                
#                 # Check if all slots are filled
#                 if available_slots() == 0:

#                     await lock_channel(constants.REGISTRATION_CHANNEL_ID)

#                     # Create and save the CSV file
#                     await save_as_csv(constants.registered_teams, 'registered_teams.csv')
                    
#                     # Send the CSV file to the designated channel
#                     channel = bot.get_channel(constants.MOD_CHANNEL_ID)
#                     if channel:
#                         await channel.send(file=discord.File('registered_teams.csv'))
#                     else:
#                         print("Error: Designated channel not found.")

#                     # Allocate lobby channels
#                     await allocate_lobby_channels()

#             else:
#                 print("All slots are filled.")
#                 await reject_registration(user_id, "Sorry, all slots are filled.")
#                 await message.add_reaction('❌')
#         else:
#             print("User is already registered.")
#             await reject_registration(user_id, "Your team has already been registered for today.")
#             await message.add_reaction('❌')
#     else:
#         print("User is not enrolled.")
#         await reject_registration(user_id, f"Your team is not enrolled yet, please do checkout <#{constants.INFO_CHANNEL_ID}>.")
#         await message.add_reaction('❌')

async def enrollTeam(user,interaction):

    # Set the flag to indicate that a process is running
    async with constants.running_processes_lock:
        constants.running_processes[user.id] = True
    thread = await create_private_thread(user, "enroll")

    try:
        # Loop until a unique team name is provided
        while True:
            # Get the team name from the user
            team_name = await get_user_response_in_thread(user, thread, "Please enter your team name:")

            # Check if the team name is empty
            if not team_name or team_name == None:
                raise ValueError("Team name cannot be empty.")
            
            # Check if the team name is banned or on cooldown
            if team_name.lower() in ["cooldown", "banned", "left_server"]:
                await thread.send(f"{user.mention} This team name is not allowed. Please choose a different team name.")
                continue

            # Check for simple mentions or newlines
            if "<@" in team_name or "@" in team_name or "\n" in team_name:
                await thread.send(f"{user.mention} Team names cannot contain mentions or multiple lines.")
                continue

            # Check if the team name has more than sufficient char
            if int(len(team_name)) > 25:
                await thread.send(f"{user.mention} Too long team name, Please choose a different team name.")
                continue

            # Check if the team name already exists
            if not await is_team_name_unique(team_name):
                await thread.send(f"{user.mention} This team name already exists.\nYou can modify the team name slightly for it to pass.\nEx: Team Chambal Ke Daku can be written any way like ChambalKeDaku, Chambal Daket, Chambal ESP, Team Chambal, Chambal Squad. Let's restart your enrollment, my friend!")
            else:
                break  # Exit the loop if a unique team name is provided

        # Get IGNs from players
        player_igns = []
        for i in range(1, 5):
            if i == 1:
                embed = discord.Embed(description="All players' in-game names (IGN) must include a team acronym as prefix/suffix (SAME NAME TAG). Players without this will not be allowed in the lobby.", color=0x229db7)  
                player_ign = await get_user_response_in_thread(user, thread, f"Please enter Player {i}'s IGN:",embed=embed)
            else:
                player_ign = await get_user_response_in_thread(user, thread, f"Please enter Player {i}'s IGN:")

            if not player_ign:
                raise ValueError(f"Player {i}'s IGN cannot be empty.")
            if "\n" in player_ign:
                raise ValueError(f"Player {i}'s IGN cannot contain multiple lines.")
            if "<@" in player_ign or "@" in player_ign:
                raise ValueError(f"Player {i}'s IGN cannot contain mentions.")
            
            player_igns.append(player_ign)

        # Ask if there is a fifth player
        fifth_player_response = await ask_yes_no_question_in_thread(user, thread, "Do you have a fifth player?")
        if fifth_player_response == 'yes':
            player5_ign = await get_user_response_in_thread(user, thread, "Please enter Player 5's IGN:")
            if not player5_ign:
                raise ValueError("Player 5's IGN cannot be empty.")
            if "\n" in player5_ign:
                raise ValueError("Player 5's IGN cannot contain multiple lines.")
            if "<@" in player5_ign or "@" in player5_ign:
                raise ValueError("Player 5's IGN cannot contain mentions.")
            player_igns.append(player5_ign)

        # Write registration details to Google Sheets
        await send_registration_details(user, team_name, player_igns, thread)
        validate_result = await validate_enrollment(user, team_name, player_igns, thread)

        # Delete thread if validation fails
        if not validate_result:
            await thread.delete()
            return

    except EnrollmentError as ee:
        # Schedule the deletion of the thread 
        async with asyncio.TaskGroup() as task_group:
            if ee.text:
                task_group.create_task(thread.send(ee.text))
                task_group.create_task(asyncio.sleep(int(ee.timeout)))  # default = 5 minutes
            else:
                task_group.create_task(thread.send(f"This thread will be deleted in {(int(ee.timeout)/60)} minutes"))
                task_group.create_task(asyncio.sleep(int(ee.timeout)))  # default = 5 minutes

        await thread.delete()

    except asyncio.TimeoutError:
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Enrollment timed out. \nPlease try again later."))
            task_group.create_task(thread.delete())

    except ValueError as ve:
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An error occurred during enrollment: {ve}"))
            task_group.create_task(thread.delete())

    except Exception as e:
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An unexpected error occurred during enrollment: {e}"))
            task_group.create_task(thread.delete())

    finally:
        # Reset the flag once the process is finished for this user
        async with constants.running_processes_lock:
            constants.running_processes.pop(user.id, None)
        await interaction.message.edit(view=TournamentView())

async def create_private_thread(user, name_suffix):
    # Create the private thread with the provided suffix
    thread_name = f"{user.name}-{name_suffix}"
    private_thread = await bot.get_guild(constants.GUILD_ID).get_channel(constants.ENROLLMENT_CHANNEL_ID).create_thread(name=thread_name,invitable=True)

    # Add the user to the private thread
    await private_thread.add_user(user)

    # Return the private thread
    return private_thread

async def updateTeam(user, existing_team_message,interaction):

    # Set the flag to indicate that a process is running
    async with constants.running_processes_lock:
        constants.running_processes[user.id] = True
    thread = await create_private_thread(user, "update")

    try:
        await thread.send(existing_team_message)
        
        # Send a message to confirm the user's decision to update their team
        confirmation_message = "Are you sure you want to update your team?\nYou will need to add complete details again of each player if you continue."

        response = await ask_yes_no_question_in_thread(user, thread, confirmation_message)
        if response == 'yes':
            try:
                await delete_team_from_sheet(user.id,constants.GOOGLE_SHEET_ID)
            except Exception as e:
                await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Error occurred while deleting team data from Google Sheets: {e}")
                await thread.delete()
                return

            async with constants.running_processes_lock:
                constants.running_processes.pop(user.id, None)

            await thread.send("Your previous team data was deleted so even if you are timed out from here, you will need to start enrollment fresh.\n\nLet's Start new enrollment! Check new mention, Separate channel has been created, clearin' this channel is 5 minutes.")

            await enrollTeam(user,interaction)
            await asyncio.sleep(300)
            await thread.delete()
            
        else:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user} Update cancelled.")
            await thread.delete()
            return
 
    except asyncio.TimeoutError:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Update timed out. Please try again later.")
        await thread.delete()
    except ValueError as ve:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An error occurred during update: {ve}")
        await thread.delete()
    except Exception as e:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An unexpected error occurred during update: {e}")
        await thread.delete()
        
    finally:
        # Reset the flag once the process is finished for this user
        async with constants.running_processes_lock:
            constants.running_processes.pop(user.id, None)
        await interaction.message.edit(view=TournamentView())
        
async def deleteTeam(user, existing_team_message,interaction):

    # Set the flag to indicate that a process is running
    async with constants.running_processes_lock:
        constants.running_processes[user.id] = True
    thread = await create_private_thread(user, "delete")

    try:
        await thread.send(existing_team_message)
        
        confirmation_message = "Are you sure you want to delete your team?\nIt cant be reverted later on and all details from our end will be lost."

        response = await ask_yes_no_question_in_thread(user, thread, confirmation_message)
        if response == 'yes':
            async with constants.running_processes_lock:
                constants.running_processes[user.id] = False

            try:
                await delete_team_from_sheet(user.id,constants.GOOGLE_SHEET_ID)
            except Exception as e:
                await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Error occurred while deleting team data from Google Sheets: {e}")
                await thread.delete()
                return

            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Your team was deleted successfully.")
            await thread.delete()
            return
        else:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user} Delete cancelled.")
            await thread.delete()
            return
        
    except asyncio.TimeoutError:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Delete timed out. Please try again later.")
        await thread.delete()
    except ValueError as ve:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An error occurred during delete: {ve}")
        await thread.delete()
    except Exception as e:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An unexpected error occurred during delete: {e}")
        await thread.delete()
 
    finally:
        # Reset the flag once the process is finished for this user
        async with constants.running_processes_lock:
            constants.running_processes.pop(user.id, None)
        await interaction.message.edit(view=TournamentView())

class EnrollmentError(Exception):
    def __init__(self, timeout=300,text=""):
        self.timeout = timeout
        self.text = text

async def get_user_response(user, prompt=""):
    try:
        await user.send(prompt)
        response = await bot.wait_for('message', check=lambda msg: msg.author == user and msg.channel.type == discord.ChannelType.private, timeout=120)
        if response.content.strip():  # Check if the response is not empty after stripping whitespace
            return response.content
        else:
            await user.send("Please provide a non-empty response.")
            return await get_user_response(user, prompt)  # Ask for response again recursively
    except asyncio.TimeoutError:
        await user.send("Response timed out. Please try again later.")
        return None

async def get_user_response_in_thread(user, channel, prompt="", timeout=300, return_message_object=False,embed=None,return_first_member=False):
    await channel.send(prompt,embed = embed)
    response = await bot.wait_for('message', check=lambda msg: msg.author == user and msg.channel == channel, timeout = int(timeout))
    if response.content.strip():  # Check if the response is not empty after stripping whitespace
        if return_message_object:
            return response  # Return the message object if requested
        if return_first_member:
            members = response.mentions[:1]
            member = members[0]
            return member
        else:
            return response.content  # Return the content of the message by default
    else:
        await channel.send("Please provide a non-empty response.")
        return await get_user_response_in_thread(user, channel, prompt, return_message_object)  # Ask for response again recursively

# async def ask_yes_no_question(user, question):
#     try:
#         # Ask the user the question
#         await user.send(question + " (yes/no)")

#         # Wait for the user's response with a timeout of 2 minutes (120 seconds)
#         response = await asyncio.wait_for(
#             bot.wait_for('message', check=lambda msg: msg.author == user and msg.channel.type == discord.ChannelType.private),
#             timeout=120
#         )

#         # Get the content of the response and convert it to lowercase
#         response = response.content.lower()

#         # Check if the response is either 'yes' or 'no'
#         if response in ['yes', 'no']:
#             return response
#         else:
#             await user.send("Enter either 'yes' or 'no'.")
#             # Recursively call the function to ask the question again
#             return await ask_yes_no_question(user, question)
            
#     except asyncio.TimeoutError:
#         # Handle the case when the timeout occurs
#         await user.send("Response timed out. Please try again later.")
#         return None

async def ask_yes_no_question_in_thread(user, channel, question):
    try:
        # Ask the user the question in the thread
        await channel.send(question + " (yes/no)")

        # Wait for the user's response with a timeout of 2 minutes (120 seconds)
        response = await asyncio.wait_for(
            bot.wait_for('message', check=lambda msg: msg.author == user and msg.channel == channel),
            timeout=300
        )

        # Get the content of the response and convert it to lowercase
        response = response.content.lower()

        # Check if the response is either 'yes' or 'no'
        if response in ['yes', 'no']:
            return response
        else:
            await channel.send("Enter either 'yes' or 'no'.")
            # Recursively call the function to ask the question again
            return await ask_yes_no_question_in_thread(user, channel, question)
            
    except asyncio.TimeoutError:
        # Handle the case when the timeout occurs
        await channel.send("Response timed out. Please try again later.")
        return None

async def send_registration_details(user, team_name, player_igns, thread):
    await thread.send(f"**Copy the meta data beneath**")
    registration_details = f"## Team {team_name}\n"
    for ign in player_igns:
        registration_details += f"{ign} -\n"

    await thread.send(registration_details)

async def validate_enrollment(user, team_name, player_igns, thread):

    if thread:
        existing_team_message = ""

        # Wait for user response with timeout

        embed = discord.Embed()
        embed.set_image(url="https://cdn.discordapp.com/attachments/1203357142548094977/1257239145752039474/download.gif?ex=66845772&is=668305f2&hm=dfa572f9b7382118aa8cca97415a5d425b7a994d4653cd0ae79689e796f8b171&")

        response = await get_user_response_in_thread(user, thread, f"Now fill up the details mentioning players against their IGNs like this example beneath and send it here.\n_Go ahead, mention your teammates now∆_", 600,True,embed=embed)  # Timeout set to 10 minutes (600 seconds)
        
        if response is None:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Validation timeout reached. Please reapply.")
            return False

        # Process user response
        mentioned_users = response.mentions
        players = [user for user in mentioned_users[:5]]
        player_discord_ids = [str(user.id) for user in mentioned_users[:5]]
        
        # Check if any of the mentioned players are already enrolled
        for discord_id in player_discord_ids:
            
            if discord_id in constants.blk_users_list:
                await thread.send(f"Your enrollment can't proceed as user : <@{discord_id}> from your team is blacklisted as of now.")
                await response.add_reaction("❌")
                raise EnrollmentError(60)
            
            text = await isAlreadyEnrolled(discord_id)
            if text:
                existing_team_message += f"Your enrollment can't proceed as either You or One of your teammate is already a part of some other team:\n"
                existing_team_message += text
                existing_team_message += f"\nIf they're not a part of listed team, reach out to the support team via <#{constants.HELP_CHANNEL_ID}>."
                await thread.send(existing_team_message)
                await response.add_reaction("❌")
                raise EnrollmentError

        # Check if at least 4 users are mentioned
        if len(players) < 4:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Hey {user.mention}, you missed mentioning all your teammates.\nPlease restart the enrollment process and mention correctly next time.\nThis message can also be sent if someone from your team is not present in this server.")
            await response.add_reaction("❌")
            raise EnrollmentError(60)
        
        # Check if at least 4 mentioned users have the required role
        unverified_players = []
        for player in players:

            # for verify wala lafda :
            if player:

                # if not any(role.name == constants.REQUIRED_ROLE_NAME for role in player.roles):
                #     unverified_players.append(player.mention)
                continue

            else:
                await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} There was some error and due to it we arent able to fetch <@{discord_id}>, report to support team if he's present in this server and still this comes.")
                await response.add_reaction("❌")
                raise EnrollmentError(60)
            
        if (len(players) == 5 and len(unverified_players) > 1) or (len(players) == 4 and len(unverified_players) >= 1):
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} One or more of your teammates haven't verified on the discord server yet. Reapply once it's done.\n(Aap verified ho aapke teammates nahi hai)")
            await response.add_reaction("❌")
            raise EnrollmentError(90,text= f"Your teammates {','.join(unverified_players)} haven't yet claimed the “Verified” role on discord. Ask them to wait until there website verification is complete/share the details via <#{constants.TICKET_CHANNEL_ID}>.\nAtleast 4 players from your team need to have verified role to create a team.")
        
        # All validation checks passed
        await response.add_reaction("✅")
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Enrollment for team **{team_name}** validated.")

        # Write enrollment details to Google Sheets
        await write_to_sheet(user.id, team_name, player_igns, (str(user.id) for user in mentioned_users[:5]))
        await thread.send("This thread will be deleted in 1 minute")
        await asyncio.sleep(60)
        await thread.delete()
        return True

    else:
        print("Validation thread not found.")
        return False
    
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
    
    # Append the row to the Google Sheets
    constants.sheet.append_row(row)
    
    # Print registration details for verification
    print(f"Registered: {initiator_idstr}, Team: {team_name}, Discord Usernames: {', '.join(player_discord_ids)}, IGNs: {', '.join(player_igns)}")

async def delete_team_from_sheet(user_id, spreadsheet_id,ctx = None):
    try:
        # Fetch all values from the worksheet
        sheet = constants.service.spreadsheets()
        result = sheet.values().get(spreadsheetId=spreadsheet_id, range="Sheet1").execute()
        values = result.get('values', [])
        
        # Find the row index and the row containing the user_id
        row_index, row = next(((i + 1, row) for i, row in enumerate(values) if str(user_id) in row), (None, None))  

        if row_index is not None:
            # Build the request payload
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

            # Execute the request to delete the row
            response = sheet.batchUpdate(
                spreadsheetId=spreadsheet_id,
                body=request_body
            ).execute()

            if not int(user_id) == int(row[0]):
                await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Hey <@{row[0]}> some of your teammate just deleted/updated your team. This is just an alert message(no need to worry) as the team was created by you.")

            print(f"Team data deleted successfully.\n{row}")
            if ctx:
                return row
        else:
            print("Team data not found for deletion.")

    except Exception as e:
        print("Error occurred while deleting team data from Google Sheets:", e)

# Function to check if a team name already exists in the Google Sheets
async def is_team_name_unique(team_name):
    try:
        team_names = constants.sheet.col_values(2)  # Assuming team names are in the first column
        return team_name not in team_names
    except Exception as e:

        # Get the JSON key file path from an environment variable
        json_keyfile_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "default_path")
        
        # for pc
        if json_keyfile_path == "default_path":
            # If the environment variable is not set, use a default path
            json_keyfile_path = "D:/Google Cloud JSON Key/clear-healer-415920-7abc2cda379e.json"

        constants.sheet, constants.service = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.GOOGLE_SHEET_ID)
        constants.ban_sheet, _ = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.BAN_SHEET_ID)
        constants.blacklist_sheet, _ = await connect_to_google_sheets(json_keyfile_path, sheet_id=constants.BLACKLIST_SHEET_ID)

        await asyncio.sleep(1)
        team_names = constants.sheet.col_values(2)  # Assuming team names are in the first column
        return team_name not in team_names

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
                message = f"# **Team Name:** {team_name}\n"

                if ctx_is_in_team:
                    for i, (name, discord_id) in enumerate(zip(player_igns[:4], discord_ids[:4]), 1):
                        message += f"{i}. **{name}** -> <@{discord_id}>\n"

                    # Check for a fifth player
                    if len(discord_ids) > 4 and discord_ids[4] is not None:
                        fifth_ign = player_igns[4]
                        fifth_discord_id = discord_ids[4]
                        if fifth_ign and fifth_discord_id:
                            message += f"5. **{fifth_ign}** -> <@{fifth_discord_id}>\n"

                else:
                    for i, discord_id in enumerate(discord_ids[:4], 1):
                        message += f"{i}. **P{i}**: <@{discord_id}>\n"

                    if len(discord_ids) >= 5 and discord_ids[4].strip():
                        message += f"5. **P5**: <@{discord_ids[4]}>\n"

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

# Function to fetch data from the worksheet and update the cache
def refresh_cache():
    initialized = False
    while True:
        try:
            # Fetch all values from the worksheet
            rows = constants.sheet.get_all_values()
            # Update the cached data
            with constants.cache_data_thread_lock:
                constants.cached_data = rows
            
            # Check if cached_data is initialized and print message only once
            if constants.cached_data is not None and not initialized:
                print(f"\ncache_data initialized.")
                initialized = True  # Set flag to True after printing
            
        except Exception as e:
            print("Error occurred while refreshing cache:", e)
        # Sleep for 5 seconds before refreshing again
        time.sleep(5)

# Function to fetch data from the worksheet and update banned_team_list
def refresh_cache2():
    initialized = False
    while True:
        try:
            # Fetch all values from the worksheet
            rows = constants.ban_sheet.get_all_values()
            with constants.ban_list_thread_lock:
                constants.banned_team_list = [row[0] for row in rows[1:]]
                
            # Check if banned_teams_list is initialized and print message only once
            if constants.banned_team_list is not None and not initialized:
                print(f"\nbanned_teams_list initialized.")
                initialized = True  # Set flag to True after printing

        except Exception as e:
            print("Error occurred while refreshing banned_teams", e)
        # Sleep for 20 seconds before refreshing again
        time.sleep(20)

# Function to fetch data from the worksheet and update the cache
def refresh_cache3():
    initialized = False
    while True:
        try:

            rows = constants.blacklist_sheet.get_all_values()
            with constants.blk_list_thread_lock:
                constants.blk_users_list = [row[0] for row in rows[1:]]

            # Check if blk_users_list is initialized and print message only once
            if constants.blk_users_list is not None and not initialized:
                print(f"\nblk_users_list initialized.")
                initialized = True  # Set flag to True after printing 

        except Exception as e:
            print("Error occurred while refreshing blk_users_list", e)
        # Sleep for 20 seconds before refreshing again
        time.sleep(5)

def refresh_cache4():
    initialized = False
    while True:
        try:
            rows = constants.cooldown_sheet.get_all_values()
            with constants.cd_list_thread_lock:
                constants.cd_team_list = [row[0] for row in rows[1:]]

            # Check if blk_users_list is initialized and print message only once
            if constants.cd_team_list is not None and not initialized:
                print(f"\ncd_users_list initialized.")
                initialized = True  # Set flag to True after printing 

        except Exception as e:
            print("Error occurred while refreshing blk_users_list", e)
        # Sleep for 20 seconds before refreshing again
        time.sleep(5)

async def validate_registration(user,check_cooldown = True,check_left_server = True, user_idd = None):
    try:
        if user == "None" and user_idd:
            user_id = user_idd
        else:
            user_id = user.id
        guild = bot.get_guild(int(constants.GUILD_ID))

        # Use the cached data to validate registration
        if constants.cached_data:
            for row in constants.cached_data:
                if str(user_id) in row:
                    
                    # Check if any player in the team has a banned or cooldown role
                    for discord_id in row[2::2]: # Discord IDs are in even indices
                        if discord_id and discord_id.isdigit():
                            member = guild.get_member(int(discord_id))
                            if member:
                                continue
                                # for role in member.roles:
                                #     if role.id == constants.COOLDOWN_ROLE_ID and check_cooldown:
                                #         # print(f"Someone from User {user_id} wali team is on cooldown.")
                                #         return 'cooldown'
                                    # elif role.id == constants.BANNED_ROLE_ID:
                                    #     print(f"Someone from User {user_id} wali team has a banned role.")
                                    #     return 'banned'
                            elif check_left_server and not member: return 'left_server'

                    else:
                        # If no player has a banned or cooldown role, return the team name
                        team_name = row[1]
                        return team_name
        
        # If cached data is not available, fetch fresh data
        else:
            constants.cached_data = constants.sheet.get_all_values()
            for row in constants.cached_data:
                if str(user_id) in row:
                    
                    # Check if any player in the team has a banned or cooldown role
                    for discord_id in row[2::2]: # Discord IDs are in even indices
                        if discord_id and discord_id.isdigit():
                            member = bot.get_user(discord_id)
                            if member:
                                continue
                                # for role in member.roles:
                                #     if role.id == constants.COOLDOWN_ROLE_ID and check_cooldown:
                                #         # print(f"Someone from User {user_id} wali team is on cooldown.")
                                #         return 'cooldown'
                                    # elif role.id == constants.BANNED_ROLE_ID:
                                    #     print(f"Someone from User {user_id} wali team has a banned role.")
                                    #     return 'banned'
                            elif check_left_server and not member: return 'left_server'

                    else:
                        # If no player has a banned or cooldown role, return the team name
                        team_name = row[1]
                        return team_name
        
        # If the user's team is not found, return None
        return None
    except Exception as e:
        print("Error occurred while checking Discord ID:", e)
        return None
    
# Function to check the number of available slots
def available_slots(lobby_number):
    # Subtract the number of registered teams from the total slots limit
    # return constants.SLOTS_LIMIT - len(constants.registered_teams)
    return constants.LOBBY_SIZE - len(constants.lobby_teams[int(lobby_number)-1])

def available_slots2(lobby_number):
    # Subtract the number of registered teams from the total slots limit
    # return constants.SLOTS_LIMIT - len(constants.registered_teams)
    return constants.SPECIAL_LOBBY_SIZE - len(constants.special_lobby_teams[int(lobby_number)-1])

# Function to reject the registration
async def reject_registration(user_id, reason):
    try:
        # Fetch the discord.User object corresponding to the user_id
        user = await bot.fetch_user(user_id)
        # Implement your logic to reject the registration
        await bot.get_channel(constants.SCRIMS_LOG_CHANNEL_ID).send(f"{user.mention} {reason}")
    except Exception as e:
        print(f"An error occurred while confirming registration: {e}")

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

# Function to lock a channel
async def lock_channel(channel_id):
    channel = bot.get_channel(channel_id)
    if channel:
        default_role = channel.guild.default_role
        await channel.set_permissions(default_role, send_messages=False)
        await channel.send("This channel has been locked.")
    else:
        print("Channel not found.")

# Function to unlock a channel
async def unlock_channel(channel_id):
    channel = bot.get_channel(channel_id)
    if channel:
        default_role = channel.guild.default_role
        await channel.set_permissions(default_role, send_messages=True)
        await channel.send("This channel has been unlocked.")
    else:
        print("Channel not found.")

# async def allocate_lobby_channels():
#     # Initialize a list to store dictionaries for each lobby
#     lobby_teams = [{} for _ in range(int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE))]

#     # List to hold users whose registrations were not confirmed
#     unconfirmed_users = []

#     copy_dict = constants.registered_teams.copy()

#     # Allocate users with preferences to their preferred lobbies
#     for user_id, team_name in copy_dict.items():
#         if user_id in constants.preferences_dict:
#             preferred_lobbies = constants.preferences_dict[user_id]
#             allocated = False

#             for lobby_number_str in preferred_lobbies:
#                 lobby_number = int(lobby_number_str)
#                 if len(lobby_teams[lobby_number - 1]) < constants.LOBBY_SIZE:  # Adjusted index
#                     lobby_teams[lobby_number - 1][user_id] = team_name  # Adjusted index
#                     await assign_team_to_lobby(user_id, team_name, lobby_number)
#                     allocated = True
#                     break

#             del constants.registered_teams[user_id]    

#             if not allocated:
#                 unconfirmed_users.append((user_id, team_name))

#     # Notify users whose registrations were not confirmed
#     for user_id, team_name in unconfirmed_users:
#         await reject_registration(user_id, "We weren't able to find a match for your lobby preference, so your slot was not confirmed.")
#         await bot.get_channel(constants.MOD_CHANNEL_ID).send(f"LAFDA MISHAP PARESHANI, yaar {bot.get_guild(constants.GUILD_ID).get_member(user_id).mention} ki Team {team_name} ki vajah se ek slot empty rahega for sure, preference f for my rememberance")

#     # Make another copy of registered_teams for safe iteration of remaining users
#     remaining_teams = constants.registered_teams.copy()

#     # Allocate remaining users to available lobbies
#     for user_id, team_name in remaining_teams.items():
#         for lobby_number_str, lobby_teams_dict in enumerate(lobby_teams):
#             lobby_number = int(lobby_number_str)
#             if len(lobby_teams_dict) < constants.LOBBY_SIZE:
#                 lobby_teams[lobby_number][user_id] = team_name
#                 await assign_team_to_lobby(user_id, team_name, lobby_number + 1)  # Adjusted index
#                 del constants.registered_teams[user_id]
#                 break

#     # Check if all allocation processes are completed
#     if not constants.registered_teams:
#         # Generate CSV files for each lobby
#         for lobby_number, lobby_teams_dict in enumerate(lobby_teams, 1):
#             csv_file = f"lobby_{lobby_number}_teams.csv"
#             await save_as_csv(lobby_teams_dict, csv_file)
#             await bot.get_channel(constants.MOD_CHANNEL_ID).send(file=discord.File(csv_file))
#             user_ids = list(lobby_teams_dict.keys())
#             team_names = [lobby_teams_dict[user_id] for user_id in user_ids]
#             await send_slots_list(team_names, discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"lobby-{lobby_number}"))

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

async def add_team_slotlist(team_name,member,channel, use_alt_lobby=None):

    # New team to add
    new_team = {team_name: member.id}

    # Auto-detect which lobby system based on channel name if not specified
    if use_alt_lobby is None:
        # RegistrationView3 uses 't3-idp-X' channel names
        # RegistrationView2 uses 'group-X-idp' channel names
        use_alt_lobby = channel.name.startswith('t3')

    # Extract the channel number from the matching channel name
    if use_alt_lobby:
        # Format: t3-idp-{number}
        channel_number = int(channel.name.split('-')[-1])
    else:
        # Format: group-{number}-idp
        channel_number = int(channel.name.split('-')[1])
    
    # Use different JSON files for different registration views to avoid collision
    json_file_name = f"alt_lobby_{channel_number}_teams.json" if use_alt_lobby else f"lobby_{channel_number}_teams.json"

    try:
        with open(json_file_name, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}

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

    with open(json_file_name, 'w') as f:
        json.dump(data, f, indent=1)

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

async def share_lobby_results(lobby_number, team_name):
    with open(f"lobby_{lobby_number}_teams.json", 'r') as f:
        teams_json = json.load(f)

        user_id = teams_json[team_name]

        t3_role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name= f"T3 Verified")
        member = bot.get_guild(constants.GUILD_ID).get_member(user_id)
        await member.add_roles(t3_role)

        channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).text_channels, name=f"group-{lobby_number}-idp")

        await channel.send(f"Results updated in <#{constants.RESULTS_CHANNEL_ID}>\n\n{team_name} : <@{user_id}>\n\nPromoted to Tier 3.")

        # team_name = await validate_registration(user="None", check_cooldown = False,check_left_server = False,user_idd=user_id)
        # if team_name in constants.cd_team_list:
        #     return f"{team_name} is already in cooldown."
        # elif not team_name:
        #     return f"Can't find any team for user: {member.mention}"
        
        # local_tz = datetime.datetime.now().astimezone().tzinfo
        # today = datetime.datetime.now(local_tz).weekday()

        # days_until_sunday = 6 - today
        # # If today is Sunday (weekday() == 6), adjust to return 0
        # if days_until_sunday == 0:
        #     days_until_sunday = 0

        # row = [team_name,int(time.time()),int((0 * 3600) + (days_until_sunday * 86400)),datetime.datetime.now(tz=constants.timezone).strftime("%Y-%m-%d %H:%M"),f"{days_until_sunday} days {0} hours",str(user_id)]
        # constants.cooldown_sheet.append_row(row)

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
    embed = discord.Embed(title=f"GROUP {lobby_number} SLOTS LIST:", description=slots_list_message,color=0x229db7)

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
    
    await lobby_channel.send(f"{lobby_role.mention}\n\nAll players IGN must have TEAM TAG included, otherwise you will be kicked from the room.\nYou can even play from new id but this is required.")
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

    # Get the JSON key file path from an environment variable
    json_keyfile_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "default_path")
    
    # for pc
    if json_keyfile_path == "default_path":
        # If the environment variable is not set, use a default path
        json_keyfile_path = "D:/Google Cloud JSON Key/clear-healer-415920-7abc2cda379e.json"

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
    bot.run(os.environ.get('DISCORD_TOKEN'))