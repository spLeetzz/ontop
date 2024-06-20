# # constants.py
import asyncio
import threading
import pytz

# Discord channel IDs
# ENROLLMENT_CHANNEL_ID = 1252296433495965788
# HELP_CHANNEL_ID = 1188847709780705431
# REGISTRATION_CHANNEL_ID = 1252296674739490908
# INFO_CHANNEL_ID = 1252296325668798485
# UPDATES_CHANNEL_ID = 1252296256462524477
# # ANNOUNCEMENTS_CHANNEL_ID = 1247539598712766534
# MOD_CHANNEL_ID = 1201507236934066226
# TEAM_RECORDS_CHANNEL_ID = 1252296601628840036
# # SCRIMS_LOG_CHANNEL_ID = 1238329036409671711
# # PREF_SELECTION_CHANNEL_ID = 1241547499416588409

# # Other constants
# SLOTS_LIMIT = 3
# LOBBY_SIZE = 1
# NUM_LOBBIES = int(SLOTS_LIMIT/LOBBY_SIZE)
# REQUIRED_ROLE_NAME = "T3 verified"
# GUILD_ID = 1187405344226426930
# COOLDOWN_ROLE_ID = 1252301248254709770
# BANNED_ROLE_ID = 1252301654557196360
# ENROLLMENT_MESSAGE_ID = 1252312771073409128
# PREFERENCE_MESSAGE_ID = None
# REG_MESSAGE_ID = 1252312771375399122
# SCRIMS_INFO_MESSAGE_ID = 1252312772096823303

# # Google Sheets details
# GOOGLE_SHEET_ID = "1yJozWjOMMc9uIhobk0xtyODWUInV7GWwQXOZlydU0i8"
# BAN_SHEET_ID = "1Loe4O0zdVVtAdrhFxSZpk6iSsOor4s5PjFSbebxcBFA"

# disabled_status = True #(RegistrationView)
# # Initialize a list to store dictionaries for each lobby
# lobby_teams = [{} for _ in range(int(SLOTS_LIMIT // LOBBY_SIZE))]
# lobby_locks = [asyncio.Lock() for _ in range(int(SLOTS_LIMIT // LOBBY_SIZE))]
# running_processes_lock = asyncio.Lock()
# cache_data_thread_lock = threading.Lock()
# ban_list_thread_lock = threading.Lock()
# registered_teams = {}
# preferences_dict = {}
# REGISTRATION_PROMPT = ""
# cached_data = None
# running_processes = {}
# captcha_question_variables = []
# banned_team_list = []
# sheet = None
# ban_sheet = None
# service = None
# timezone = pytz.timezone('Asia/Kolkata')
# emotes_list = ["<:number1:1252296980638597221>", "<:number2:1252297093926883411>", "<:number3:1252297208624058541>", "<:number4:1252297299200049234>"]
# practice_emoteid = "<:Holdgun:1252297519543877684>"
# stary_emote = "<a:_:1188860052187119707>"


# # for i ama naive server

ENROLLMENT_CHANNEL_ID = 1238661754519814216
HELP_CHANNEL_ID = 1238661883222036520
REGISTRATION_CHANNEL_ID = 1238662128102014977
INFO_CHANNEL_ID = 1247535665755852932
UPDATES_CHANNEL_ID = 1238662241906200670
ANNOUNCEMENTS_CHANNEL_ID = 1247539598712766534
MOD_CHANNEL_ID = 1246058128613834784
TEAM_RECORDS_CHANNEL_ID = 1238254933833421002
SCRIMS_LOG_CHANNEL_ID = 1238329036409671711
PREF_SELECTION_CHANNEL_ID = 1241547499416588409

# Other constants
SLOTS_LIMIT = 3
LOBBY_SIZE = 1
NUM_LOBBIES = int(SLOTS_LIMIT/LOBBY_SIZE)
REQUIRED_ROLE_NAME = "new role"
GUILD_ID = 798542556970614816
COOLDOWN_ROLE_ID = 1231821911667900537
BANNED_ROLE_ID = 1236442273718341764
ENROLLMENT_MESSAGE_ID = 1246472440263999491
PREFERENCE_MESSAGE_ID = None
REG_MESSAGE_ID = 1246816052604698695
SCRIMS_INFO_MESSAGE_ID = 1247554882873851992

# Google Sheets details
GOOGLE_SHEET_ID = "1yJozWjOMMc9uIhobk0xtyODWUInV7GWwQXOZlydU0i8"
BAN_SHEET_ID = "1Loe4O0zdVVtAdrhFxSZpk6iSsOor4s5PjFSbebxcBFA"

disabled_status = True #(RegistrationView)
# Initialize a list to store dictionaries for each lobby
lobby_teams = [{} for _ in range(int(SLOTS_LIMIT // LOBBY_SIZE))]
lobby_locks = [asyncio.Lock() for _ in range(int(SLOTS_LIMIT // LOBBY_SIZE))]
running_processes_lock = asyncio.Lock()
cache_data_thread_lock = threading.Lock()
ban_list_thread_lock = threading.Lock()
registered_teams = {}
preferences_dict = {}
REGISTRATION_PROMPT = ""
cached_data = None
running_processes = {}
captcha_question_variables = []
banned_team_list = []
sheet = None
ban_sheet = None
service = None
timezone = pytz.timezone('Asia/Kolkata')
emotes_list = ["<:number1:1246465778480447611>", "<:number2:1246465786202034326>", "<:number3:1246465780845908079>", "<:number4:1246465783530389614>"]
practice_emoteid = "<:Untitleddesign20:1246793790199431231>"
stary_emote = "<a:_:1247699959411638282>"
