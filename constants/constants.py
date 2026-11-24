# constants.py
import asyncio
import threading
import pytz

# Discord channel IDs
ENROLLMENT_CHANNEL_ID = 1322867084387942420
HELP_CHANNEL_ID = 1188847709780705431
REGISTRATION_CHANNEL_ID = 1252296674739490908
INFO_CHANNEL_ID = 1331900777995698216
UPDATES_CHANNEL_ID = 1252296256462524477
TEAM_RECORDS_CHANNEL_ID = 1252296601628840036
TICKET_CHANNEL_ID = 1187407524983480461
HOW_TO_PLAY_CHANNEL_ID = 1259394375880802434
RESULTS_CHANNEL_ID = 1188850520803262565
STAFF_CHANNEL_ID = 1522643481036718131

GROUP_LOBBY_MAP = {
    "A": [1, 2, 3, 4],
    "B": [5, 6, 7, 8],
}

GROUP_LOBBY_MAP2 = {
    "A": [1],
    "B": [2],
    "C": [3],
}

group_locks = {
    "A": asyncio.Lock(), "B": asyncio.Lock(),
}

group_locks2 = {
    "A": asyncio.Lock(),
    "B": asyncio.Lock(),
    "C": asyncio.Lock(),
}

GROUP_LABELS = {
    "A": "3PM",
    "B": "7PM",
}

GROUP_LABELS2 = {
    "A": "3PM",
    "B": "7PM",
    "C": "9PM"
}

# Other constants
MAX_GROUP_REGISTRATIONS = 2  # max groups a team can register in (open registration) = 1 lobby per group
MAX_T3_GROUP_REGISTRATIONS = 1  # max groups a team can register in (T3 registration) = 1 lobby max per day

# Cancel slot deadlines (IST, 24hr format), team can cancel until this time
CANCEL_DEADLINES = {
    "A": "14:30", "B": "18:30",
}
CANCEL_DEADLINES_T3 = {
    "A": "14:30", "B": "18:30", "C": "20:30"
}

SLOTS_LIMIT = 160
LOBBY_SIZE = 20
REQUIRED_ROLE_NAME = "T3 verified"
GUILD_ID = 1187405344226426930
ENROLLMENT_MESSAGE_ID = 1558362570912702546
REG_MESSAGE_ID = 1558362578642804807
SCRIMS_INFO_MESSAGE_ID = 1354370354809929771
FAQ_MESSAGE_ID = 1260069527538896997

SPECIAL_SLOTS_LIMIT = 60
SPECIAL_LOBBY_SIZE = 20
special_registered_teams = {}
special_registered_set = set()
special_lobby_teams = [{} for _ in range(int(SPECIAL_SLOTS_LIMIT // SPECIAL_LOBBY_SIZE))]

special_registration_lock = asyncio.Lock()

SPECIAL_REGISTRATION_CHANNEL_ID = 1513409916352594054
SPECIAL_REG_MESSAGE_ID = 1558362575182237737

special_disabled_status = True

# Google Sheets details
GOOGLE_SHEET_ID = "1dwy7jgeT8gfqWZ7J1CPgcnMC8RuUxgv-fqrHrJwGUDY"
BAN_SHEET_ID = "1Loe4O0zdVVtAdrhFxSZpk6iSsOor4s5PjFSbebxcBFA"
BLACKLIST_SHEET_ID = "1iCWcUOOhp3957_NL860_Cf5YQwgtdbPc3EQGa_VAA7E"
COOLDOWN_SHEET_ID = "1H-PUeegORmUKQmIn6t7qSk4GIIP5TFFoEofl6GC37yo"

disabled_status = True  # Open registration closed
registered_set = set()
lobby_teams = [{} for _ in range(int(SLOTS_LIMIT // LOBBY_SIZE))]
running_processes_lock = asyncio.Lock()
registration_lock = asyncio.Lock()
cancel_slots_lock = asyncio.Lock()  # lock for cancel/claim slot operations
cache_data_thread_lock = threading.Lock()
ban_list_thread_lock = threading.Lock()
blk_list_thread_lock = threading.Lock()
cd_list_thread_lock = threading.Lock()
registered_teams = {}
cached_data = None
running_processes = {}
temp_json_dict = {}
temp_json_dict2 = {}
captcha_question_variables = []
banned_team_list = []
blk_users_list = []
cd_team_list = []
ban_until = {}        # team -> unix time the ban ends (None = no end recorded)
cd_until = {}         # team -> unix time the cooldown ends
blk_until = {}        # user id -> unix time the blacklist ends
reg_started_at = {"open": None, "t3": None}
late_attempts = {"open": 0, "t3": 0}
days_to_run = {1, 2, 3, 4, 5}  # Tue-Sat only (no Sun/Mon)
sheet = None
ban_sheet = None
blacklist_sheet = None
cooldown_sheet = None
service = None
timezone = pytz.timezone('Asia/Kolkata')
practice_emoteid = "<:Holdgun:1252297519543877684>"
roles_for_purge_perm = ['Admin','++D']
roles_for_bot_access = ['Admin','++D','Scrim Managers']
idp_role_names = [f"Group {x} IDP" for x in range(1,int(SLOTS_LIMIT // LOBBY_SIZE) + 1)]
# Match schedule (IST). idt = when the room ID goes out, st = match start.
# Every lobby plays M1 Rondo, M2 Erangel, M3 Miramar.
match_schedule = {
    1: {
        1: {"idt": "3:00 PM", "st": "3:05 PM", "map": "RONDO"},
        2: {"idt": "3:40 PM", "st": "3:45 PM", "map": "ERANGEL"},
        3: {"idt": "4:20 PM", "st": "4:25 PM", "map": "MIRAMAR"}
    },
    2: {
        1: {"idt": "3:10 PM", "st": "3:15 PM", "map": "RONDO"},
        2: {"idt": "3:50 PM", "st": "3:55 PM", "map": "ERANGEL"},
        3: {"idt": "4:30 PM", "st": "4:35 PM", "map": "MIRAMAR"}
    },
    3: {
        1: {"idt": "3:20 PM", "st": "3:25 PM", "map": "RONDO"},
        2: {"idt": "4:00 PM", "st": "4:05 PM", "map": "ERANGEL"},
        3: {"idt": "4:40 PM", "st": "4:45 PM", "map": "MIRAMAR"}
    },
    4: {
        1: {"idt": "3:30 PM", "st": "3:35 PM", "map": "RONDO"},
        2: {"idt": "4:10 PM", "st": "4:15 PM", "map": "ERANGEL"},
        3: {"idt": "4:50 PM", "st": "4:55 PM", "map": "MIRAMAR"}
    },
    5: {
        1: {"idt": "7:00 PM", "st": "7:05 PM", "map": "RONDO"},
        2: {"idt": "7:40 PM", "st": "7:45 PM", "map": "ERANGEL"},
        3: {"idt": "8:20 PM", "st": "8:25 PM", "map": "MIRAMAR"}
    },
    6: {
        1: {"idt": "7:10 PM", "st": "7:15 PM", "map": "RONDO"},
        2: {"idt": "7:50 PM", "st": "7:55 PM", "map": "ERANGEL"},
        3: {"idt": "8:30 PM", "st": "8:35 PM", "map": "MIRAMAR"}
    },
    7: {
        1: {"idt": "7:20 PM", "st": "7:25 PM", "map": "RONDO"},
        2: {"idt": "8:00 PM", "st": "8:05 PM", "map": "ERANGEL"},
        3: {"idt": "8:40 PM", "st": "8:45 PM", "map": "MIRAMAR"}
    },
    8: {
        1: {"idt": "7:30 PM", "st": "7:35 PM", "map": "RONDO"},
        2: {"idt": "8:10 PM", "st": "8:15 PM", "map": "ERANGEL"},
        3: {"idt": "8:50 PM", "st": "8:55 PM", "map": "MIRAMAR"}
    }
}
match_schedule_t3 = {
    1: {
        1: {"idt": "3:00 PM", "st": "3:05 PM", "map": "RONDO"},
        2: {"idt": "3:40 PM", "st": "3:45 PM", "map": "ERANGEL"},
        3: {"idt": "4:20 PM", "st": "4:25 PM", "map": "MIRAMAR"}
    },
    2: {
        1: {"idt": "7:00 PM", "st": "7:05 PM", "map": "RONDO"},
        2: {"idt": "7:40 PM", "st": "7:45 PM", "map": "ERANGEL"},
        3: {"idt": "8:20 PM", "st": "8:25 PM", "map": "MIRAMAR"}
    },
    3: {
        1: {"idt": "9:00 PM", "st": "9:05 PM", "map": "RONDO"},
        2: {"idt": "9:40 PM", "st": "9:45 PM", "map": "ERANGEL"},
        3: {"idt": "10:20 PM", "st": "10:25 PM", "map": "MIRAMAR"}
    }
}
