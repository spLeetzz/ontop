import asyncio
import datetime
import discord
from discord.ext import commands,tasks
from discord import app_commands
from discord.ext.commands import MissingPermissions
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from pandas import DataFrame
import logging
import time
import os
import threading
import csv
import random
from string import ascii_lowercase
from constants import constants
import datetime
import json

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
            
            elif not any(role.name == constants.REQUIRED_ROLE_NAME for role in user.roles):
                await interaction.response.send_message(f"Hey {user.mention} you are not verified yet, please complete that first.\nIf verified ho and still this pops up, claim your verification role from <#{constants.TICKET_CHANNEL_ID}>", ephemeral=True,delete_after=30)
                await interaction.message.edit(view=TournamentView())
                return
            
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

async def send_remenu(channel):
    embed = discord.Embed(title="BookMySlot", description=f"*Hey Wanderer, can I lurk on you :>*\n\n**OPENS AT 12 PM TUE-SAT**\n\n1. Make sure that you have completed the enrollment of your team from this channel <#{constants.ENROLLMENT_CHANNEL_ID}>\n2. Please book a slot only if you wanna participate in the scrims, there wont be any slot cancellation/reassignment later on.\n3. Fastest ones to register in any lobby will be allocated with the slots.\n4. You need to pass in a simple Captcha test for registration, have a look at it anytime with 'TRIAL REG' button.", color=0x229db7)
    # 2. One team can participate once in a week, cooldowns refresh every Tuesday.
    view = RegistrationView()  
    message = await channel.send(embed=embed, view=view)
    return message

async def send_overview_menu(channel):
    view = ScrimsOverviewView()  
    message = await channel.send(content=f"Hey there, here's a complete overview of BGMI scrims at Trident:\n\nThere are 4 tiers basically,\n\n`Trident Rookie Scrims(Tier 3):`\n\n- Open for all, anyone can participate, registrations open at 12 PM Tuesday-Saturday in <#{constants.REGISTRATION_CHANNEL_ID}>\n- 8 Groups every day, Top 1 from each Group qualify for Amateur Scrims\n- Every Group plays 2 matches, Erangle-Miramar\n\n`Amateur Scrims(T2 filtration):`\n\n- 2 Groups on Sunday, Top 4 from each Group qualify for Tier 2 scrims\n- Every Group plays 3 matches, Erangle-Miramar-Sanhok\n\n`Trident Elite Scrims(Tier 2):`\n\n- 4 Groups, Every team plays 24 matches over 6 days.\n- Tuesday-Sunday daily 4 matches, Erangle-Miramar-Sanhok-Vikendi\n- Top 6 teams based on cumulative leaderboard of both Groups qualify for Pro Scrims.\n- Bottom 12 teams are demoted from Tier 2 to Tier 3.\n\n`Trident Pro Scrims:`\n\n- Tuesday-Sunday daily 4 matches, Erangle-Miramar-Sanhok-Vikendi\n- Top 6 teams based on leaderboard retain their spots in Pro Scrims.\n- Rest of the teams are demoted from Pro Scrims to Tier 2.\n\nAny announcements and updates would be shared thru the <#1188849982632099940> channels.\nReact with buttons beneath for more information and make sure to follow all rules.",view=view)
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
        super().__init__(label=f'Lobby {lobby_number}', style=discord.ButtonStyle.green,disabled=constants.disabled_status,emoji=f"{constants.emotes_list[lobby_number-1]}",row = 1 if lobby_number >= 5 else 0)
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

            elif team_name in constants.registered_teams.keys():
                    await interaction.response.send_message("Someone from your team has already booked a slot for today.", ephemeral=True,delete_after=120)
            
            elif user_id not in constants.registered_teams and await available_slots(self.lobby_number) > 0:
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
            #     slots_available_currently = await available_slots(self.lobby_number)
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
            async with constants.lobby_locks[int(int(self.lobby_number) - 1)]:
                
                slots_available_currently = await available_slots(self.lobby_number)

                if int(slots_available_currently) <= 0:
                    self.slots_available = False
                
                elif self.team_name in constants.registered_teams.keys() and self.slots_available:
                    self.already_registered = True
                
                elif self.slots_available and not self.already_registered:
                    # Save the registered team's data
                    constants.registered_teams[self.team_name] = await isAlreadyEnrolled(user_id,used2returnrow=True)
                    constants.lobby_teams[int(self.lobby_number)-1][self.team_name] = user_id

            if not self.slots_available:
                await interaction.followup.send("Sorry, this lobby is full.", ephemeral=True) 
                await save_timestamp_to_csv(interaction.user, timestamp_ms,self.lobby_number,"LATE")
                return
            
            if self.already_registered:
                await interaction.followup.send("Someone from your team has already booked a slot for today.", ephemeral=True)
                return
                
            # Operations that do not need to be locked
            if await available_slots(self.lobby_number) == 0:
                await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).send(f"Slots filled in Lobby {self.lobby_number} at time:\n{timestamp_ms}")

            task1 = asyncio.create_task(interaction.followup.send(f"Registration confirmed for “{self.team_name}” in Lobby {self.lobby_number}.", ephemeral=True))
            # task2 = asyncio.create_task(assign_role(user, constants.COOLDOWN_ROLE_ID))
            task3 = asyncio.create_task(assign_team_to_lobby(user, self.lobby_number))
            task4 = asyncio.create_task(save_timestamp_to_csv(user, timestamp_ms, self.lobby_number,"BOOKED"))

            # await asyncio.gather(task1,task2,task3,task4)
            await asyncio.gather(task1,task3,task4)

            async with constants.registration_lock:
                if len(constants.registered_teams) == constants.SLOTS_LIMIT:
                    if not constants.disabled_status:
                        constants.disabled_status = True
                        message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
                        await message.edit(view=RegistrationView())
                        await save_as_csv(constants.registered_teams, 'registered_teams.csv',save_all_flag = True)
                        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File('registered_teams.csv'))

                        for lobby_number, lobby_teams_dict in enumerate(constants.lobby_teams, 1):
                            # csv_file = f"lobby_{lobby_number}_teams.csv"
                            # await save_as_csv(lobby_teams_dict, csv_file)

                            json_file_name = f"lobby_{lobby_number}_teams.json"
                            # Write the data dictionary to a JSON file
                            with open(json_file_name, 'w') as f:
                                json.dump(lobby_teams_dict, f, indent=1)

                            team_names = list(lobby_teams_dict.keys())
                            user_ids = [lobby_teams_dict[team_name] for team_name in team_names]
                            async with asyncio.TaskGroup() as taskhandler:
                                # taskhandler.create_task(bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File(csv_file)))
                                taskhandler.create_task(bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File(json_file_name)))
                                try:
                                    idp_channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"group-{lobby_number}-idp")
                                    await send_slots_list(team_names, lobby_number,idp_channel)
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

class PracticeRegistrationButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f'TRIAL REG', style=discord.ButtonStyle.primary,emoji=constants.practice_emoteid,row=2)

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
        
class RegistrationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(1, (int(constants.SLOTS_LIMIT/constants.LOBBY_SIZE)) + 1):
            self.add_item(LobbyButton(i))
        self.add_item(PracticeRegistrationButton())

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
        matching_roles = [role for role in interaction.user.roles if role.name in constants.idp_role_names]

        if len(matching_roles) == 1:
            try:
                result = await isAlreadyEnrolled(interaction.user.id,used2returnrowwithmessage=True,ctx_is_in_team=True)
                if result:
                    await interaction.response.send_message(f"{result[0]}\nSelect player in the dropdown below whom you wanna transfer this IDP role to.",view=PlayerSelectView(row = result[1],role = matching_roles[0]),ephemeral=True,delete_after=120)
                else:
                    await interaction.response.send_message(f"Unable to retrieve any team associated with you, did you deleted it?",ephemeral=True,delete_after=40)
            except Exception as e:
                print(e)

        elif len(matching_roles) == 0:
            await interaction.response.send_message(f"You dont Got any role that can be transferred:.",ephemeral=True,delete_after=40)

        else:
            await interaction.response.send_message(f"You have like more than one Lobbies roles, i am comfused and can not handle role transfers for you.",ephemeral=True,delete_after=40)

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
            benificar = bot.get_guild(constants.GUILD_ID).get_member(selected_value)
            await user.remove_roles(self.role)
            await asyncio.sleep(2)
            await assign_role(benificar,self.role.id)
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"Hey {user.mention}, Your lobby role has been transferred to your teammate {benificar}")
            await interaction.response.send_message("Done.", ephemeral=True,delete_after=30)
            
        except discord.errors.Forbidden:
            await interaction.response.send_message("I do not have permission to manage your roles.", ephemeral=True,delete_after=30)
        except discord.errors.HTTPException:
            await interaction.response.send_message("Failed to remove role due to a Discord API error.", ephemeral=True,delete_after=60)
        except Exception as e:
            await interaction.response.send_message(f"Got some error: {e}", ephemeral=True,delete_after=60)

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
            lobby_number = interaction.channel.name.split('-')[1]
            lobby_number_int = int(lobby_number)
            
            if 1 <= lobby_number_int <= (int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)):
                
                temp_dict =  None

                with open(f"lobby_{lobby_number}_teams.json", 'r') as f:
                    temp_dict = json.load(f)

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
            lobby_number = interaction.channel.name.split('-')[1]
            lobby_number_int = int(lobby_number)
            
            if 1 <= lobby_number_int <= (int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)):
                
                temp_dict =  None

                with open(f"lobby_{lobby_number}_teams.json", 'r') as f:
                    temp_dict = json.load(f)

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

        if any(role.name in constants.roles_for_purge_perm for role in interaction.user.roles):

            await interaction.response.send_message(f"on itt...",ephemeral=True,delete_after=3)
            try:
                mod_channel = bot.get_channel(constants.UPDATES_CHANNEL_ID) 
                team_name = await get_user_response_in_thread(
                    interaction.user,
                    mod_channel,
                    f"{interaction.user.mention} let me know the team's name."
                )

                id_of_member_associated = await get_user_response_in_thread(
                    interaction.user,
                    mod_channel,
                    f"{interaction.user.mention} mention the user you wanna add.",return_first_mention=True
                )
                
                # New team to add
                new_team = {team_name: id_of_member_associated}

                # Extract the channel number from the matching channel name
                channel_number = interaction.channel.name.split('-')[1]
                json_file_name = f"lobby_{channel_number}_teams.json"

                with open(json_file_name, 'r+') as f:
                    data = json.load(f)   # Read the existing JSON data into a dictionary
                    data.update(new_team)    # Add the new team to the dictionary
                    f.seek(0)             # Move the file pointer to the beginning of the file
                    json.dump(data, f, indent=1)   # Write the updated dictionary back to the file
                    f.truncate()          # Ensure the file is truncated to the new length

                team_names = list(data.keys())
                async with asyncio.TaskGroup() as taskhandler:
                    try:
                        await send_slots_list(team_names, channel_number, interaction.channel,edit_slots_list= True)
                    except Exception as e:
                        print(f"Got Exception: {e}")
                        
                role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name= f"Group {channel_number} IDP")
                await interaction.guild.get_member(id_of_member_associated).add_roles(role)
                await mod_channel.send("Done babu")

            except asyncio.TimeoutError:
                pass
            except Exception as e:
                await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"Got an Exception: {e}")
        else:
            await interaction.response.send_message(f"You can't use this command my bruhh.",ephemeral=True,delete_after=40)
    
class IdpChannelTasksView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TransferIDPButton())
        self.add_item(ModToolsButton())

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

async def validate_captcha(captcha_phrase : str, sum1_answer : int, sum2_answer : int):
    # Placeholder for actual captcha validation logic
    return (captcha_phrase.lower().rstrip() == constants.captcha_question_variables[0] and sum1_answer == int(constants.captcha_question_variables[1] + constants.captcha_question_variables[2]) and sum2_answer == int(constants.captcha_question_variables[3] + constants.captcha_question_variables[4]))  # Replace with actual validation
    
# Event handler for when the bot is ready
@bot.event
async def on_ready():
    
    print(f"We have logged in as {bot.user} but wait we ain't ready")

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

    if constants.REG_MESSAGE_ID and bot.get_channel(constants.REGISTRATION_CHANNEL_ID):
        try:
            # Fetch the message
            message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
        except discord.NotFound:
            # If the message is not found, handle the case gracefully
            message = None

        if message:
            # Edit the existing message with the dropdown menu
            await message.edit(view=RegistrationView())
        else:
            # Send a new message with the dropdown menu
            message = await send_remenu(bot.get_channel(constants.REGISTRATION_CHANNEL_ID))
        
        # Update the interaction message ID
        constants.REG_MESSAGE_ID = message.id

    else:
        # If the channel or message ID is not valid, send the select menu
        message = await send_remenu(bot.get_channel(constants.REGISTRATION_CHANNEL_ID))
        constants.REG_MESSAGE_ID = message.id

    if constants.SCRIMS_INFO_MESSAGE_ID and bot.get_channel(constants.INFO_CHANNEL_ID):
        try:
            # Fetch the message
            message = await bot.get_channel(constants.INFO_CHANNEL_ID).fetch_message(constants.SCRIMS_INFO_MESSAGE_ID)
        except discord.NotFound:
            # If the message is not found, handle the case gracefully
            message = None

        if message:
            # Edit the existing message with the dropdown menu
            await message.edit(view=ScrimsOverviewView())
        else:
            # Send a new message with the dropdown menu
            message = await send_overview_menu(bot.get_channel(constants.INFO_CHANNEL_ID))
        
        # Update the interaction message ID
        constants.SCRIMS_INFO_MESSAGE_ID = message.id

    else:
        # If the channel or message ID is not valid, send the select menu
        message = await send_overview_menu(bot.get_channel(constants.INFO_CHANNEL_ID))
        constants.SCRIMS_INFO_MESSAGE_ID = message.id

    if constants.SCRIMS_INFO_MESSAGE_ID and bot.get_channel(constants.INFO_CHANNEL_ID):
        try:
            # Fetch the message
            message = await bot.get_channel(constants.INFO_CHANNEL_ID).fetch_message(constants.SCRIMS_INFO_MESSAGE_ID)
        except discord.NotFound:
            # If the message is not found, handle the case gracefully
            message = None

        if message:
            # Edit the existing message with the dropdown menu
            await message.edit(view=ScrimsOverviewView())
        else:
            # Send a new message with the dropdown menu
            message = await send_overview_menu(bot.get_channel(constants.INFO_CHANNEL_ID))
        
        # Update the interaction message ID
        constants.SCRIMS_INFO_MESSAGE_ID = message.id

    else:
        # If the channel or message ID is not valid, send the select menu
        message = await send_overview_menu(bot.get_channel(constants.INFO_CHANNEL_ID))
        constants.SCRIMS_INFO_MESSAGE_ID = message.id

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

    with open('lobby_details.json', 'r') as f:
        lobby_details_json = json.load(f)

#     await bot.get_channel(constants.HOW_TO_PLAY_CHANNEL_ID).send(f"""1. Get verified on [Trident Gaming](<https://tridentgaming.in/account>) after entering the required details. Verification is a manual process and may take around 1-2 weeks. (All 4 players of your team must be verified from your team in order to play)

# *Aapko [Trident Gaming](<https://tridentgaming.in/account>) website pe jaakar aadhar and baaki details upload update krni hai apni profile me and apne saare teammates se bhi ye karwana hai, fir aapka profile "Under Verification" show hoga, ye "Verified" hone me 1-2 weeks lagege and apko wait krna rahega.*""")

#     await bot.get_channel(constants.HOW_TO_PLAY_CHANNEL_ID).send(f"""2. Once you're verified, hover over to <#{constants.TICKET_CHANNEL_ID}> channel and select "T3 Verification" from the dropdown there, you'll be added to a private channel, send your aadhar card number and a screenshot showing that you are "Verified" on Trident website to claim your discord role. (All 4 players of your team have to claim role on discord by same procedure)

# *Jab aap website par "Verified" ho jao fir <#{constants.TICKET_CHANNEL_ID}> wale channel me jaana and "Click Here" pe click karke "T3 Verification" wala option select krna, fir aapko ek channel me add kr diya jayega jaha aapko apna aadhar number and ek screenshot bhejna jisme ye clearly dikhe ki aap Trident website pe "Verified" ho. Ye kriya aapke saare dosto ko krni hai and ye krne se aapko discord role mil jayega.*\n\n3. Enroll your team from <#{constants.ENROLLMENT_CHANNEL_ID}> , just have to select "Enroll my team" option from there, fill simple details, mention your teammates, and you're fine to Go.
#    You can even Update/Delete your team later on.

# 4. Book your slot for your preferred lobby from <#{constants.REGISTRATION_CHANNEL_ID}> at 12 PM Tuesday-Saturday. The buttons there will remain disabled whole time, and will open up at registration time.

# *3rd and 4th step samjhne ke liye ek baar aap niche wali video dekhlo, bas ek baar team banani h aapko baar baar mention nai krna h apne dosto ko and ek simple captcha fill krne se aapka registration hoga.*""")

    if lobby_details_json:
        for k,v in lobby_details_json.items():
            try:
                message = await bot.get_channel(int(v[1])).fetch_message(int(v[0]))
                await message.edit(view=IdpChannelTasksView())
            except Exception as e:
                print(f"Got Exception {e} when delaing with lobby json file")

    # start_auto.start()
    clear_lb_auto.start()
    idploop.start()
    idploop2.start()
    idploop3.start()
    idploop4.start()
    idploop5.start()
    idploop6.start()
    t3rulesreminder.start()
    t3rulesreminder2.start()
    
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

@bot.hybrid_command(name="start",description="To Start REG, the captcha you pass in will be default for everyone.")
@commands.has_permissions(view_audit_log=True, manage_roles=True)
async def start(ctx, captcha_phrase : str):

    await ctx.defer()
    constants.registered_teams.clear()
    constants.lobby_teams = [{} for _ in range(int(int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)))]
    constants.disabled_status = False
    constants.captcha_question_variables.clear()

    try:
        await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).purge(check=lambda m: m.id != constants.REG_MESSAGE_ID, limit=100)
        print("Messages purged successfully.")
        # Clear timestamps.csv
        with open('timestamps.csv', 'w', newline=''): pass
        message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
        await message.edit(view=RegistrationView())

        with open('lobby_details.json','w') as json_file:
            json.dump({},json_file)

    except discord.HTTPException as e:
        print(f"An error occurred while purging messages: {e}")

    constants.captcha_question_variables.append(captcha_phrase.lower().rstrip())
    constants.captcha_question_variables.append(random.randint(10, 99))
    constants.captcha_question_variables.append(random.randint(10, 99))
    constants.captcha_question_variables.append(random.randint(10, 99))
    constants.captcha_question_variables.append(random.randint(10, 99))
    
    await ctx.send(f"refresheeeeeeeeeeeeeeeeeeeed\nCurrent captcha variables: {constants.captcha_question_variables[0]}, {constants.captcha_question_variables[1]} + {constants.captcha_question_variables[2]}, {constants.captcha_question_variables[3]} + {constants.captcha_question_variables[4]}")

@start.error
async def start_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="break",description="**Break Registration in between, Sensitive")
@commands.has_permissions(view_audit_log=True, manage_roles=True)
async def break_reg(ctx):

    await ctx.defer()
    constants.disabled_status = True
    message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
    await message.edit(view=RegistrationView())
    await save_as_csv(constants.registered_teams, 'registered_teams.csv',save_all_flag = True)
    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File('registered_teams.csv'))

    for lobby_number, lobby_teams_dict in enumerate(constants.lobby_teams, 1):
        # csv_file = f"lobby_{lobby_number}_teams.csv"
        # await save_as_csv(lobby_teams_dict, csv_file)

        json_file_name = f"lobby_{lobby_number}_teams.json"
        # Write the data dictionary to a JSON file
        with open(json_file_name, 'w') as f:
            json.dump(lobby_teams_dict, f, indent=1)

        team_names = list(lobby_teams_dict.keys())
        user_ids = [lobby_teams_dict[team_name] for team_name in team_names]
        async with asyncio.TaskGroup() as taskhandler:
            # await bot.get_channel(constants.MOD_CHANNEL_ID).send(file=discord.File(csv_file))
            await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(file=discord.File(json_file_name))
            try:
                idp_channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"group-{lobby_number}-idp")
                await send_slots_list(team_names, lobby_number,idp_channel)
#                 pov_message = """Hello Teams,

# Please follow these steps to record your Point of View (POV) while playing BGMI:
# 1. Before opening the BGMI app show the list of background running apps on your device
# 2. Go to the PlayStore (for Android users) or Appstore (for iOS users) and open the BGMI app.
# 3. Join the Lobby using the provided details.
# 4. Before starting the match, ensure that both your in-game audio and your own voice (microphone) are being recorded.
# 5. Play the match.
# 6. After each match, make sure to show the list of background running apps and IMEI on your device.
# 7. You need to repeat the above steps for every match you play."""
#                 await idp_channel.send(pov_message)
            except Exception as e:
                print(f"Got Exception when sending lobby csv files: {e}")
                
    await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"You can download the Google Sheets app to view the list of users and their registration timestamps of {datetime.datetime.today().strftime('%d %b')} from this CSV file (for transparency). If you cant find you name in these, you were later than all these 😢.",file=discord.File('timestamps.csv'))

    with open('lobby_details.json','w') as json_file:
        json.dump(constants.temp_json_dict,json_file,indent=1)

    await ctx.send(f"breaked REG in between")

@break_reg.error
async def break_reg_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="editslots", description="Change the slots limit and lobby size.")
@commands.has_permissions(view_audit_log=True, manage_roles=True)
async def editslots(ctx, slots_limit: int, lobby_size: int):
    await ctx.defer()

    constants.SLOTS_LIMIT = slots_limit
    constants.LOBBY_SIZE = lobby_size
    constants.lobby_teams = [{} for _ in range(int(slots_limit / lobby_size))]

    try:
        message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
        await message.edit(view=RegistrationView())

    except discord.HTTPException as e:
        print(f"An error occurred while purging messages: {e}")

    await ctx.send(f"Slots limit and lobby size updated:\nSlots limit: {slots_limit}\nLobby size: {lobby_size}")

@editslots.error
async def editslots_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="show_limits", description="Show the current slots limit and lobby size.")
@commands.has_permissions(manage_roles=True)
async def show_limits(ctx):
    await ctx.send(f"Current slots limit: {constants.SLOTS_LIMIT}\nCurrent lobby size: {constants.LOBBY_SIZE}")

@bot.hybrid_command(name="delete_from_sheet",description="**SENSITIVE, this team's data can be lost forever from our end.")
@commands.has_any_role('++D','Admin','.',"Mahatma")
async def delete_from_sheet(ctx,member: discord.User):
    try:
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

@bot.hybrid_command(name="clearlb", description="**Clear lobby Channels and role")
@commands.has_permissions(view_audit_log=True, manage_roles=True)
async def clear_lb(ctx):

    await ctx.send("kr rha thoda wait krna ..")

    lobby_role_names = [f"Group {i} IDP" for i in range(1, int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE) + 1)]
    lobby_channel_names = [f"group-{i}-idp" for i in range(1, int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE) + 1)]

    try:
        for role_name in lobby_role_names:
            role = discord.utils.get(ctx.guild.roles, name=role_name)
            if role:
                for member in role.members:
                    await member.remove_roles(role)

        # role = discord.utils.get(ctx.guild.roles, name="Amateur IDP 1")
        # if role:
        #     for member in role.members:
        #         await member.remove_roles(role)

        # role = discord.utils.get(ctx.guild.roles, name="Amateur IDP 2")
        # if role:
        #     for member in role.members:
        #         await member.remove_roles(role)

        for channel_name in lobby_channel_names:
            channel = discord.utils.get(ctx.guild.channels, name=channel_name)
            if channel:
                await channel.purge(after=(datetime.datetime.now() - datetime.timedelta(hours=24)),before=ctx.message.created_at)

        await ctx.send("Lobby channels (last 24 hrs) and roles are cleared now.")
    
    except discord.Forbidden:
        await ctx.send("I do not have permission to manage roles or channels.")
    except discord.HTTPException as e:
        await ctx.send(f"An HTTP error occurred: {e}")    
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

@clear_lb.error
async def clear_lb(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="clear_amateur", description="**Clear Amateur lobby Channels and role**")
@commands.has_any_role(*constants.roles_for_purge_perm)
@commands.has_permissions(view_audit_log=True, manage_roles=True)
async def clear_amateur(ctx):
    await ctx.send("kr rha thoda wait krna ..")

    role_names = ["Amateur IDP 1", "Amateur IDP 2"]
    channel_names = [
        "amateur-updates",
        "amateur-slotlist",
        "amateur-idp1",
        "amateur-idp2",
        "amateur-queries",
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
        for channel_name in channel_names:
            channel = discord.utils.get(ctx.guild.channels, name=channel_name)
            if channel:
                await channel.purge(limit=500, reason=f"amateur clearup by {ctx}", before=ctx.interaction.created_at)
                print(f"Purged messages from {channel_name}")

        await ctx.send("Amateur Lobby channels (last 24 hrs) and roles are cleared now.")

    except discord.Forbidden:
        await ctx.send("I do not have permission to manage roles or channels.")
    except discord.HTTPException as e:
        await ctx.send(f"An HTTP error occurred: {e}")    
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

@clear_amateur.error
async def clear_amateur(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        await ctx.send(f"You don't have the required permissions to use this command: {missing_perms}")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.hybrid_command(name="rr", description="Fetch random users who reacted to a message")
@commands.has_permissions(view_audit_log=True)
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
@commands.has_permissions(manage_messages=True,view_audit_log=True, manage_roles=True)
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
@commands.has_permissions(manage_roles=True)
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

@bot.hybrid_command(name="faq", description="send faq view")
@commands.has_permissions(manage_roles=True)
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

@bot.hybrid_command(name="inrole", description="inrole users and team names")
@commands.has_permissions(manage_roles=True,view_audit_log=True)
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
async def inrole(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("The specified channel was not found.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.tree.command(name="amr_one", description="Grant amateur scrims role (1) thru idp channel.")
@app_commands.checks.has_permissions(manage_roles=True, view_audit_log=True)
async def amr_one(interaction: discord.Interaction, team_name : str):
    try:
        # Get the interaction channel name
        channel_name = interaction.channel.name

        # Check if the channel name matches any in the channel_role_names list
        matching_channel = next((name for name in constants.channel_names if name == channel_name), None)

        if matching_channel:
            # Extract the channel number from the matching channel name
            channel_number = matching_channel.split('-')[1]

            with open(f"lobby_{channel_number}_teams.json", 'r') as f:
                teams_json = json.load(f)

            user_id = teams_json[team_name]
            amr1 = interaction.guild.get_role(1219781255638286407)
            await interaction.guild.get_member(user_id).add_roles(amr1)

            role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name= f"Group {channel_number} IDP")

            await interaction.channel.send(f"{role.mention}\nResults have been updated in <#{constants.RESULTS_CHANNEL_ID}>\n\n{team_name} -> <@{user_id}>\n\nQualified for Amateur Scrims.")

            team_name = await validate_registration(user="None", check_cooldown = False,check_left_server = False,user_idd=user_id)
            if team_name in constants.cd_team_list:
                await interaction.response.send_message("Bhai ye team already hai cooldown me, if duration badhana h to splitz ko pakdo, aese command se krna thoda mushkil hai")
                return
            elif not team_name:
                await interaction.response.send_message("Couldn't find any team with this user.")
                return
            
            local_tz = datetime.datetime.now().astimezone().tzinfo
            today = datetime.datetime.now(local_tz).weekday()

            days_until_sunday = 6 - today
            # If today is Sunday (weekday() == 6), adjust to return 0
            if days_until_sunday == 0:
                days_until_sunday = 0

            row = [team_name,int(time.time()),int((0 * 3600) + (days_until_sunday * 86400)),datetime.datetime.now(tz=constants.timezone).strftime("%Y-%m-%d %H:%M"),f"{days_until_sunday} days {0} hours",str(user_id)]
            constants.cooldown_sheet.append_row(row)

    except discord.HTTPException as e:
        await interaction.send(f"An error occurred while sending the message: {e}")

@amr_one.error
async def amr_one(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("The specified channel was not found.")
    else:
        await ctx.send(f"An error occurred: {error}")

@bot.tree.command(name="amr_two", description="Grant amateur scrims role (2) thru idp channel.")
@app_commands.checks.has_permissions(manage_roles=True, view_audit_log=True)
async def amr_two(interaction: discord.Interaction, team_name : str):
    try:
        # Get the interaction channel name
        channel_name = interaction.channel.name

        # Check if the channel name matches any in the channel_role_names list
        matching_channel = next((name for name in constants.channel_names if name == channel_name), None)

        if matching_channel:
            # Extract the channel number from the matching channel name
            channel_number = matching_channel.split('-')[1]

            with open(f"lobby_{channel_number}_teams.json", 'r') as f:
                teams_json = json.load(f)

            user_id = teams_json[team_name]
            amr2 = interaction.guild.get_role(1247510834524192859)
            await interaction.guild.get_member(user_id).add_roles(amr2)

            role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name= f"Group {channel_number} IDP")

            await interaction.channel.send(f"{role.mention}\nResults have been updated in <#{constants.RESULTS_CHANNEL_ID}>\n\n{team_name} -> <@{user_id}>\n\nQualified for Amateur Scrims.")

            team_name = await validate_registration(user="None", check_cooldown = False,check_left_server = False,user_idd=user_id)
            if team_name in constants.cd_team_list:
                await interaction.response.send_message("Bhai ye team already hai cooldown me, if duration badhana h to splitz ko pakdo, aese command se krna thoda mushkil hai")
                return
            elif not team_name:
                await interaction.response.send_message("Couldn't find any team with this user.")
                return
            
            local_tz = datetime.datetime.now().astimezone().tzinfo
            today = datetime.datetime.now(local_tz).weekday()

            days_until_sunday = 6 - today
            # If today is Sunday (weekday() == 6), adjust to return 0
            if days_until_sunday == 0:
                days_until_sunday = 0

            row = [team_name,int(time.time()),int((0 * 3600) + (days_until_sunday * 86400)),datetime.datetime.now(tz=constants.timezone).strftime("%Y-%m-%d %H:%M"),f"{days_until_sunday} days {0} hours",str(user_id)]
            constants.cooldown_sheet.append_row(row)
        
    except discord.HTTPException as e:
        await interaction.send(f"An error occurred while sending the message: {e}")

@amr_two.error
async def amr_two(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have the required permissions to use this command.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("The specified channel was not found.")
    else:
        await ctx.send(f"An error occurred: {error}")

local_tz = datetime.datetime.now().astimezone().tzinfo
x = datetime.time(hour=12, minute=0, tzinfo=local_tz)
@tasks.loop(time=x)
async def start_auto():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        print("REG STARTED!")
        constants.registered_teams.clear()
        constants.lobby_teams = [{} for _ in range(int(int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)))]
        constants.disabled_status = False
        constants.captcha_question_variables.clear()
        captcha_phrase = ''.join(random.choice(ascii_lowercase) for _ in range(random.randint(5, 6)))

        try:
            await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).purge(check=lambda m: m.id != constants.REG_MESSAGE_ID, limit=100)
            print("Messages purged successfully.")
            # Clear timestamps.csv
            with open('timestamps.csv', 'w', newline=''): pass
            message = await bot.get_channel(constants.REGISTRATION_CHANNEL_ID).fetch_message(constants.REG_MESSAGE_ID)
            await message.edit(view=RegistrationView())

            with open('lobby_details.json','w') as json_file:
                json.dump({},json_file)

        except discord.HTTPException as e:
            print(f"An error occurred while purging messages: {e}")

        constants.captcha_question_variables.append(captcha_phrase.lower().rstrip())
        constants.captcha_question_variables.append(random.randint(10, 99))
        constants.captcha_question_variables.append(random.randint(10, 99))
        constants.captcha_question_variables.append(random.randint(10, 99))
        constants.captcha_question_variables.append(random.randint(10, 99))

        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"*REG STARTED!*\nCurrent captcha variables: {constants.captcha_question_variables[0]}, {constants.captcha_question_variables[1]} + {constants.captcha_question_variables[2]}, {constants.captcha_question_variables[3]} + {constants.captcha_question_variables[4]}")

y = datetime.time(hour=11, minute=0, tzinfo=local_tz)
@tasks.loop(time=y)
async def clear_lb_auto():

    today = datetime.datetime.now(local_tz).weekday()
    if today in constants.days_to_run:
        lobby_role_names = [f"Group {i} IDP" for i in range(1, int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE) + 1)]
        lobby_channel_names = [f"group-{i}-idp" for i in range(1, int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE) + 1)]

        for role_name in lobby_role_names:
            role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=role_name)
            if role:
                for member in role.members:
                    await member.remove_roles(role)

        for channel_name in lobby_channel_names:
            channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=channel_name)
            if channel:
                await channel.purge(after=(datetime.datetime.now() - datetime.timedelta(hours=24)))

        await bot.get_channel(constants.UPDATES_CHANNEL_ID).send(f"*CLEARED LOBBIES!*")

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
MAP- MIRAMAR

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

        lobby_number = int((int(constants.inner_loop_counter) % ((int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)))) + 4)

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

        lobby_number = int((int(constants.inner_loop_counter) % ((int(constants.SLOTS_LIMIT) / int(constants.LOBBY_SIZE)))) + 4)

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
MAP- MIRAMAR

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

rulesremindertime = datetime.time(hour=14, minute=00, tzinfo=local_tz)
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

rulesremindertime2 = datetime.time(hour=14, minute=50, tzinfo=local_tz)
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
            if team_name.lower() == "cooldown" or team_name.lower() == "banned" or team_name.lower() == "left_server":
                await thread.send(f"{user.mention} This team name is not allowed. Please choose a different team name.")

            # Check if the team name has more than sufficient char
            if int(len(team_name)) > 40:
                await thread.send(f"{user.mention} Too long team name, Please choose a different team name.")

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
                player_igns.append(player_ign)
                continue
            
            player_ign = await get_user_response_in_thread(user, thread, f"Please enter Player {i}'s IGN:")
            player_igns.append(player_ign)

            if not player_ign:
                raise ValueError(f"Player {i}'s IGN cannot be empty.")

        # Ask if there is a fifth player
        fifth_player_response = await ask_yes_no_question_in_thread(user, thread, "Do you have a fifth player?")
        if fifth_player_response == 'yes':
            player5_ign = await get_user_response_in_thread(user, thread, "Please enter Player 5's IGN:")
            if not player5_ign:
                raise ValueError("Player 5's IGN cannot be empty.")
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

async def get_user_response_in_thread(user, channel, prompt="", timeout=300, return_message_object=False,embed=None,return_first_mention=False):
    await channel.send(prompt,embed = embed)
    response = await bot.wait_for('message', check=lambda msg: msg.author == user and msg.channel == channel, timeout = int(timeout))
    if response.content.strip():  # Check if the response is not empty after stripping whitespace
        if return_message_object:
            return response  # Return the message object if requested
        if return_first_mention:
            user_ids = response.mentions[:1]
            user_id = user_ids[0]
            return user_id.id
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

                if not any(role.name == constants.REQUIRED_ROLE_NAME for role in player.roles):
                    unverified_players.append(player.mention)

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
async def available_slots(lobby_number):
    # Subtract the number of registered teams from the total slots limit
    # return constants.SLOTS_LIMIT - len(constants.registered_teams)
    return constants.LOBBY_SIZE - len(constants.lobby_teams[int(lobby_number)-1])

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

async def save_as_csv(teams_dict, csv_file,save_all_flag=False):
    if save_all_flag:
        # Extract User IDs and team names from the dictionary
        team_names = [key for key in teams_dict.keys()]
        user_ids = [','.join(teams_dict[team_name]) for team_name in team_names]

        # Create a DataFrame with User IDs and team names as columns
        df = DataFrame({'Team_Name': team_names,'User_IDS': user_ids})
        
        # Write the DataFrame to a CSV file
        df.to_csv(csv_file, index=False)  # Set index=False to exclude row numbers in the CSV file
        return
        
    # Extract User IDs and team names from the dictionary
    user_ids = [key for key in teams_dict.keys()]
    team_names = [teams_dict[user_id] for user_id in user_ids]
    
    # Create a DataFrame with User IDs and team names as columns
    df = DataFrame({'User_ID': user_ids, 'Team_Name': team_names})
    
    # Write the DataFrame to a CSV file
    df.to_csv(csv_file, index=False)  # Set index=False to exclude row numbers in the CSV file

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

async def assign_team_to_lobby(user, lobby_number):

    lobby_role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"Group {lobby_number} IDP")
    lobby_channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"group-{lobby_number}-idp")

    if lobby_role and lobby_channel:
        await user.add_roles(lobby_role)

async def send_slots_list(team_names, lobby_number, lobby_channel,edit_slots_list=  False):
    # Prepare the slots list message
    slots_list_message = "```yaml\n"
    
    # Add the first two slots as "EMPTY" and "RESERVED"
    slots_list_message += f"01. EMPTY\n"
    slots_list_message += f"02. RESERVED\n"

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
    embed = discord.Embed(title=f"GROUP {lobby_number} SLOTS LIST:", description=slots_list_message,color=0x229db7)

    if edit_slots_list:
        with open('lobby_details.json', 'r') as f:
            lobby_details_json = json.load(f)

        if lobby_details_json:
            try:
                message = await bot.get_channel(lobby_details_json[lobby_number][1]).fetch_message(lobby_details_json[lobby_number][0])
            except Exception as e:
                print(f"Got Exception {e} while fetchin' slot list message.")
            await message.edit(embed=embed,view=IdpChannelTasksView())
            return

    message = await lobby_channel.send(embed=embed)
    lobby_role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=f"Group {lobby_number} IDP")
    followupmsg = await lobby_channel.send(lobby_role.mention)
    try:
        await message.edit(view=IdpChannelTasksView())
    except Exception as e:
        print(e)
    constants.temp_json_dict[lobby_number] = [message.id,lobby_channel.id]

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