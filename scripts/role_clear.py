"""
role_clear.py — Remove stale lobby roles from members not present in the teams JSON.

For each lobby (open + T3), this script:
  1. Reads the corresponding _teams.json file.
  2. Collects the set of valid user IDs from that file.
  3. Checks every member who currently has the lobby role.
  4. Removes the role from anyone NOT in the valid set.

Usage:
    python scripts/role_clear.py          # dry-run (default, prints what it would do)
    python scripts/role_clear.py --apply  # actually removes the roles
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import discord

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("ERROR: DISCORD_TOKEN not found in .env")
    sys.exit(1)

GUILD_ID = 1187405344226426930

# Open lobbies
SLOTS_LIMIT = 320
LOBBY_SIZE = 20
NUM_OPEN_LOBBIES = int(SLOTS_LIMIT // LOBBY_SIZE)  # 16

# T3 lobbies
SPECIAL_SLOTS_LIMIT = 108
SPECIAL_LOBBY_SIZE = 18
NUM_T3_LOBBIES = int(SPECIAL_SLOTS_LIMIT // SPECIAL_LOBBY_SIZE)  # 6


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_valid_user_ids(json_path: Path) -> set[int]:
    """Return the set of user IDs from a teams JSON file.

    The JSON format is: {"team_name": user_id, ...}
    Entries with value "cancelled" are ignored.
    """
    if not json_path.exists():
        return set()

    try:
        with open(json_path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  ⚠  Could not read {json_path.name}: {exc}")
        return set()

    valid_ids = set()
    for _team_name, uid in data.items():
        if uid == "cancelled":
            continue
        try:
            valid_ids.add(int(uid))
        except (ValueError, TypeError):
            pass

    return valid_ids


def build_lobby_map() -> list[dict]:
    """Build a list of dicts describing each lobby to process.

    Each dict has:
        role_name  — the Discord role name to look up
        json_file  — Path to the corresponding _teams.json
        label      — human-readable label for logging
    """
    lobbies = []

    # Open lobbies: role = "Group {N} IDP", file = lobby_{N}_teams.json
    for i in range(1, NUM_OPEN_LOBBIES + 1):
        lobbies.append({
            "role_name": f"Group {i} IDP",
            "json_file": PROJECT_ROOT / f"lobby_{i}_teams.json",
            "label": f"Open Lobby {i}",
        })

    # T3 lobbies: role = "T3 G{N} IDP", file = alt_lobby_{N}_teams.json
    for i in range(1, NUM_T3_LOBBIES + 1):
        lobbies.append({
            "role_name": f"T3 G{i} IDP",
            "json_file": PROJECT_ROOT / f"alt_lobby_{i}_teams.json",
            "label": f"T3 Lobby {i}",
        })

    return lobbies


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(dry_run: bool = True):
    intents = discord.Intents.default()
    intents.members = True  # need members intent to iterate role.members

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user} (ID: {client.user.id})")
        guild = client.get_guild(GUILD_ID)
        if not guild:
            print(f"ERROR: Could not find guild {GUILD_ID}")
            await client.close()
            return

        lobbies = build_lobby_map()
        total_removed = 0
        total_checked = 0

        for lobby in lobbies:
            role = discord.utils.get(guild.roles, name=lobby["role_name"])
            if not role:
                print(f"[{lobby['label']}] Role '{lobby['role_name']}' not found — skipping")
                continue

            valid_ids = load_valid_user_ids(lobby["json_file"])
            json_exists = lobby["json_file"].exists()

            members_with_role = role.members
            if not members_with_role:
                print(f"[{lobby['label']}] No members have role '{role.name}' — nothing to do")
                continue

            if not json_exists:
                print(f"[{lobby['label']}] {lobby['json_file'].name} not found — "
                      f"will remove role from ALL {len(members_with_role)} members")

            print(f"\n[{lobby['label']}] Role: {role.name} | "
                  f"Members with role: {len(members_with_role)} | "
                  f"Valid user IDs from JSON: {len(valid_ids)}")

            for member in members_with_role:
                total_checked += 1
                if member.id not in valid_ids:
                    if dry_run:
                        print(f"  [DRY-RUN] Would remove '{role.name}' from "
                              f"{member} (ID: {member.id})")
                    else:
                        try:
                            await member.remove_roles(role)
                            print(f"  ✓ Removed '{role.name}' from "
                                  f"{member} (ID: {member.id})")
                        except discord.Forbidden:
                            print(f"  ✗ No permission to remove role from {member}")
                        except discord.HTTPException as exc:
                            print(f"  ✗ HTTP error removing role from {member}: {exc}")
                    total_removed += 1

        mode_label = "DRY-RUN" if dry_run else "APPLIED"
        print(f"\n{'='*50}")
        print(f"Done ({mode_label}). Checked {total_checked} member-role pairs, "
              f"{'would remove' if dry_run else 'removed'} {total_removed}.")
        if dry_run and total_removed > 0:
            print("Run with --apply to actually remove roles.")
        print(f"{'='*50}")

        await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    dry_run = "--apply" not in sys.argv
    if dry_run:
        print("🔍 Running in DRY-RUN mode (pass --apply to actually remove roles)\n")
    else:
        print("⚡ Running in APPLY mode — roles WILL be removed\n")

    asyncio.run(run(dry_run=dry_run))
