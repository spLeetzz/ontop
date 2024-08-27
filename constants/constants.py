# # constants.py
import asyncio
import threading
import pytz

# Discord channel IDs
ENROLLMENT_CHANNEL_ID = 1252296433495965788
HELP_CHANNEL_ID = 1188847709780705431
REGISTRATION_CHANNEL_ID = 1252296674739490908
INFO_CHANNEL_ID = 1252296325668798485
UPDATES_CHANNEL_ID = 1252296256462524477
# ANNOUNCEMENTS_CHANNEL_ID = 1247539598712766534
MOD_CHANNEL_ID = 1201507236934066226
TEAM_RECORDS_CHANNEL_ID = 1252296601628840036
TICKET_CHANNEL_ID = 1187407524983480461
# SCRIMS_LOG_CHANNEL_ID = 1238329036409671711
# PREF_SELECTION_CHANNEL_ID = 1241547499416588409
HOW_TO_PLAY_CHANNEL_ID = 1259394375880802434
RESULTS_CHANNEL_ID = 1188850520803262565

# Other constants
SLOTS_LIMIT = 160
LOBBY_SIZE = 20
REQUIRED_ROLE_NAME = "T3 verified"
GUILD_ID = 1187405344226426930
COOLDOWN_ROLE_ID = 1252301248254709770
BANNED_ROLE_ID = 1252301654557196360
ENROLLMENT_MESSAGE_ID = 1257508668316848158
PREFERENCE_MESSAGE_ID = None
REG_MESSAGE_ID = 1269813332357939252
SCRIMS_INFO_MESSAGE_ID = 1277847304455323669
FAQ_MESSAGE_ID = 1260069527538896997

# Google Sheets details
GOOGLE_SHEET_ID = "1yJozWjOMMc9uIhobk0xtyODWUInV7GWwQXOZlydU0i8"
BAN_SHEET_ID = "1Loe4O0zdVVtAdrhFxSZpk6iSsOor4s5PjFSbebxcBFA"
BLACKLIST_SHEET_ID = "1gKvcIl_gM3HQrUqq0B2O7CYTKLf8DgBtFfFMsD4Kp8s"
COOLDOWN_SHEET_ID = "1H-PUeegORmUKQmIn6t7qSk4GIIP5TFFoEofl6GC37yo"

disabled_status = True #(RegistrationView)
# Initialize a list to store dictionaries for each lobby
lobby_teams = [{} for _ in range(int(SLOTS_LIMIT // LOBBY_SIZE))]
lobby_locks = [asyncio.Lock() for _ in range(int(SLOTS_LIMIT // LOBBY_SIZE))]
running_processes_lock = asyncio.Lock()
registration_lock = asyncio.Lock()
cache_data_thread_lock = threading.Lock()
ban_list_thread_lock = threading.Lock()
blk_list_thread_lock = threading.Lock()
cd_list_thread_lock = threading.Lock()
registered_teams = {}
preferences_dict = {}
REGISTRATION_PROMPT = ""
cached_data = None
running_processes = {}
temp_json_dict = {}
captcha_question_variables = []
banned_team_list = []
blk_users_list = []
cd_team_list = []
days_to_run = {1, 2, 3, 4, 5}  # 1: Tuesday, 2: Wednesday, 3: Thursday, 4: Friday, 5: Saturday
sheet = None
ban_sheet = None
blacklist_sheet = None
cooldown_sheet = None
service = None
timezone = pytz.timezone('Asia/Kolkata')
emotes_list = ["<:number1:1252296980638597221>", "<:number2:1252297093926883411>", "<:number3:1252297208624058541>", "<:number4:1252297299200049234>","<:number5:1256671680831553617>","<:number6:1256671747374059683>","<:number7:1277844614883180604>","<:number8:1277844682881372211>"]
practice_emoteid = "<:Holdgun:1252297519543877684>"
stary_emote = "<a:_:1188860052187119707>"
roles_for_purge_perm = ['Admin','++D']
idp_role_names = [f"Group {x} IDP" for x in range(1,int(SLOTS_LIMIT // LOBBY_SIZE) + 1)]
channel_names = [f"group-{x}-idp" for x in range(1,int(SLOTS_LIMIT // LOBBY_SIZE) + 1)]
inner_loop_counter = 0
match_schedule = {
    1: {
        1: {"idt": "3:00 PM", "st": "3:05 PM"},
        2: {"idt": "4:00 PM", "st": "4:05 PM"}
    },
    2: {
        1: {"idt": "3:10 PM", "st": "3:15 PM"},
        2: {"idt": "4:10 PM", "st": "4:15 PM"}
    },
    3: {
        1: {"idt": "3:20 PM", "st": "3:25 PM"},
        2: {"idt": "4:20 PM", "st": "4:25 PM"}
    },
    4: {
        1: {"idt": "3:30 PM", "st": "3:35 PM"},
        2: {"idt": "4:30 PM", "st": "4:35 PM"}
    },
    5: {
        1: {"idt": "3:40 PM", "st": "3:45 PM"},
        2: {"idt": "4:40 PM", "st": "4:45 PM"}
    },
    6: {
        1: {"idt": "3:50 PM", "st": "3:55 PM"},
        2: {"idt": "4:50 PM", "st": "4:55 PM"}
    }
}


# # for i ama naive server

# ENROLLMENT_CHANNEL_ID = 1238661754519814216
# HELP_CHANNEL_ID = 1238661883222036520
# REGISTRATION_CHANNEL_ID = 1238662128102014977
# INFO_CHANNEL_ID = 1247535665755852932
# UPDATES_CHANNEL_ID = 1238662241906200670
# ANNOUNCEMENTS_CHANNEL_ID = 1247539598712766534
# MOD_CHANNEL_ID = 1246058128613834784
# TEAM_RECORDS_CHANNEL_ID = 1238254933833421002
# TICKET_CHANNEL_ID = 1238661883222036520
# SCRIMS_LOG_CHANNEL_ID = 1238329036409671711
# PREF_SELECTION_CHANNEL_ID = 1241547499416588409
# HOW_TO_PLAY_CHANNEL_ID = None
# RESULTS_CHANNEL_ID = None

# # Other constants
# SLOTS_LIMIT = 2
# LOBBY_SIZE = 1
# NUM_LOBBIES = int(SLOTS_LIMIT/LOBBY_SIZE)
# REQUIRED_ROLE_NAME = "new role"
# GUILD_ID = 798542556970614816
# COOLDOWN_ROLE_ID = 1231821911667900537
# BANNED_ROLE_ID = 1236442273718341764
# ENROLLMENT_MESSAGE_ID = 1254380929179189361
# PREFERENCE_MESSAGE_ID = None
# REG_MESSAGE_ID = 1246816052604698695
# SCRIMS_INFO_MESSAGE_ID = 1254380948808536226

# # Google Sheets details
# GOOGLE_SHEET_ID = "1yJozWjOMMc9uIhobk0xtyODWUInV7GWwQXOZlydU0i8"
# BAN_SHEET_ID = "1Loe4O0zdVVtAdrhFxSZpk6iSsOor4s5PjFSbebxcBFA"
# BLACKLIST_SHEET_ID = "1gKvcIl_gM3HQrUqq0B2O7CYTKLf8DgBtFfFMsD4Kp8s"
# COOLDOWN_SHEET_ID = "1H-PUeegORmUKQmIn6t7qSk4GIIP5TFFoEofl6GC37yo"

# disabled_status = True #(RegistrationView)
# # Initialize a list to store dictionaries for each lobby
# lobby_teams = [{} for _ in range(int(SLOTS_LIMIT // LOBBY_SIZE))]
# lobby_locks = [asyncio.Lock() for _ in range(int(SLOTS_LIMIT // LOBBY_SIZE))]
# running_processes_lock = asyncio.Lock()
# cache_data_thread_lock = threading.Lock()
# ban_list_thread_lock = threading.Lock()
# blk_list_thread_lock = threading.Lock()
# cd_list_thread_lock = threading.Lock()
# registered_teams = {}
# preferences_dict = {}
# REGISTRATION_PROMPT = ""
# cached_data = None
# running_processes = {}
# captcha_question_variables = []
# banned_team_list = []
# blk_users_list = []
# cd_team_list = []
# temp_json_dict = {}
# sheet = None
# ban_sheet = None
# blacklist_sheet = None
# cooldown_sheet = None
# service = None
# timezone = pytz.timezone('Asia/Kolkata')
# emotes_list = ["<:number1:1246465778480447611>", "<:number2:1246465786202034326>", "<:number3:1246465780845908079>", "<:number4:1246465783530389614>"]
# practice_emoteid = "<:Untitleddesign20:1246793790199431231>"
# stary_emote = "<a:_:1247699959411638282>"
# json_file_path = r"C:\faltu\ontop\lobby_details.json"
# roles_for_purge_perm = ['Manager','Mahatma']
# days_to_run = {1, 2, 3, 4, 5}  # 1: Tuesday, 2: Wednesday, 3: Thursday, 4: Friday, 5: Saturday
# idp_role_names = [f"Group {x} IDP" for x in range(1,int(SLOTS_LIMIT // LOBBY_SIZE))]
# channel_names = [f"group-{x}-idp" for x in range(1,int(SLOTS_LIMIT // LOBBY_SIZE) + 1)]
# inner_loop_counter = 0
# match_schedule = {
#     1: {
#         1: {"idt": "3:00 PM", "st": "3:05 PM"},
#         2: {"idt": "4:00 PM", "st": "4:05 PM"}
#     },
#     2: {
#         1: {"idt": "3:10 PM", "st": "3:15 PM"},
#         2: {"idt": "4:10 PM", "st": "4:15 PM"}
#     },
#     3: {
#         1: {"idt": "3:20 PM", "st": "3:25 PM"},
#         2: {"idt": "4:20 PM", "st": "4:25 PM"}
#     },
#     4: {
#         1: {"idt": "3:30 PM", "st": "3:35 PM"},
#         2: {"idt": "4:30 PM", "st": "4:35 PM"}
#     },
#     5: {
#         1: {"idt": "3:40 PM", "st": "3:45 PM"},
#         2: {"idt": "4:40 PM", "st": "4:45 PM"}
#     },
#     6: {
#         1: {"idt": "3:50 PM", "st": "3:55 PM"},
#         2: {"idt": "4:50 PM", "st": "4:55 PM"}
#     }
# }
