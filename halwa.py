import asyncio
import datetime
import discord
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import pandas as pd
import logging
import time
import os
import threading
from constants import constants

# Configure logging
logging.basicConfig(level=logging.INFO)

# Create a new instance of the Discord bot with command functionality
bot = commands.Bot(command_prefix='-', intents=discord.Intents().all())

# Event handler for when the bot is ready
@bot.event
async def on_ready():

    # Get the JSON key file path from an environment variable
    json_keyfile_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "default_path")
    
    # for pc
    if json_keyfile_path == "default_path":
        # If the environment variable is not set, use a default path
        json_keyfile_path = "D:/Google Cloud JSON Key/clear-healer-415920-7abc2cda379e.json"

    try:
        # Attempt to connect to Google Sheets
        constants.sheet, constants.service = connect_to_google_sheets(json_keyfile_path)
    except Exception as e:
        print("Error while connecting to Google Sheets:", e)
    
    print(f'We have logged in as {bot.user}')
    
    if constants.ENROLLMENT_MESSAGE_ID and bot.get_channel(constants.ENROLLMENT_CHANNEL_ID):
        try:
            # Fetch the message
            message = await bot.get_channel(constants.ENROLLMENT_CHANNEL_ID).fetch_message(constants.ENROLLMENT_MESSAGE_ID)
        except discord.NotFound:
            # If the message is not found, reset the interaction message ID
            constants.ENROLLMENT_MESSAGE_ID = None
            return
        # Check if the message exists
        if message:
            # Edit the message with the dropdown menu
            await message.edit(view=TournamentView())
        else:
            # Send the initial message with the dropdown menu
            message = await send_selectmenu(bot.get_channel(constants.ENROLLMENT_CHANNEL_ID))
            # Store the interaction message ID
            constants.ENROLLMENT_MESSAGE_ID = message.id
            
    else:
        # If the channel or message ID is not valid, send the select menu
        await send_selectmenu(bot.get_channel(constants.ENROLLMENT_CHANNEL_ID))

        # Handle Tournament View persistence
    if constants.PREFERENCE_MESSAGE_ID and bot.get_channel(constants.PREF_SELECTION_CHANNEL_ID):
        try:
            # Fetch the message
            message = await bot.get_channel(constants.PREF_SELECTION_CHANNEL_ID).fetch_message(constants.PREFERENCE_MESSAGE_ID)
        except discord.NotFound:
            # If the message is not found, reset the interaction message ID
            constants.PREFERENCE_MESSAGE_ID = None
            return
        if message:
            await message.edit(view=LobbyPreferencesView())
        else:
            message = await send_pref_menu(bot.get_channel(constants.PREF_SELECTION_CHANNEL_ID))
            constants.PREFERENCE_MESSAGE_ID = message.id
    else:
        # If the channel or message ID is not valid, send the select menu
        await send_pref_menu(bot.get_channel(constants.PREF_SELECTION_CHANNEL_ID))
    
def connect_to_google_sheets(json_keyfile_path, retry_interval=10):
    while True:
        try:
            credentials = Credentials.from_service_account_file(json_keyfile_path, scopes=['https://www.googleapis.com/auth/spreadsheets'])
            gc = gspread.authorize(credentials)
            print("Successfully authenticated with Google Sheets.")

            service = build('sheets', 'v4', credentials=credentials)

            sheet = gc.open_by_key(constants.GOOGLE_SHEET_ID).sheet1
            print("Successfully opened Google Sheets document by ID:", constants.GOOGLE_SHEET_ID)
            
            return sheet, service

        except Exception as e:
            print("Error while connecting to Google Sheets:", e)
            print(f"Retrying in {retry_interval} seconds...")
            time.sleep(retry_interval)

# Define the TournamentDropdown class inheriting from discord.ui.Select
class TournamentDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Enroll", value="enroll", description="Enroll your Team, GL Mate!", emoji="📑"),
            discord.SelectOption(label="Update", value="update", description="Update current Team", emoji="📝"),
            discord.SelectOption(label="Delete", value="delete", description="Delete current Team", emoji="🗑️")
        ]
        super().__init__(placeholder="Select an action", options=options)

    async def callback(self, interaction: discord.Interaction):
        # Handle the interaction response based on the selected value
        selected_value = self.values[0]
        user = interaction.user

        # Check if a process is already running
        if constants.running_processes.get(user.id):
            await interaction.response.send_message("Another process is already running. Please wait until the previous process is finished (up to 2 minutes of inactivity).", ephemeral=True,delete_after=30)
            return
        
        if selected_value == "enroll":
            # Check if the user is already enrolled
            team_details = isAlreadyEnrolled(user.id)
            if team_details:
                existing_team_message = f"{user.mention} Your enrollment can't proceed as either You or One of your teammates is already a part of some other team:\n"
                existing_team_message += team_details
                existing_team_message += f"\nIf they're not a part of the listed team, reach out to the support team via <#{constants.HELP_CHANNEL_ID}>."
                await interaction.response.send_message(existing_team_message, ephemeral=True,delete_after=300)
                return
            
            await interaction.response.send_message("Enrollment Started, Check your mentions!",ephemeral=True, delete_after=30)
            await enrollTeam(user)
            await interaction.message.edit(view=TournamentView())

        elif selected_value == "update":
            await interaction.response.send_message("Update selected, Check your mentions!",ephemeral=True, delete_after=30)
            await updateTeam(user)
            await interaction.message.edit(view=TournamentView())

        elif selected_value == "delete":
            await interaction.response.send_message("Delete selected, Check your mentions!",ephemeral=True, delete_after=30)
            await deleteTeam(user)
            await interaction.message.edit(view=TournamentView())

# Define the TournamentView class inheriting from discord.ui.View
class TournamentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Set timeout to None to make the view persistent
        self.add_item(TournamentDropdown())

async def send_selectmenu(channel):
    embed = discord.Embed(title="Team Enrollment", description="Select desirable option to create or edit your team details\n\n a. \"**Enroll**\" team to pull the ropes now, introduce your crew nd here we sail!\n\n b. \"**Update**\" your team if you are just fed of someone midway.\n\n c.\"**Delete**\" your team if stuck on some lonesome island.", color=0x229db7)
    select = TournamentDropdown()

    view = TournamentView()  # Initialize TournamentView with timeout=None
    view.add_item(select)

    await channel.send(embed=embed, view=view)

async def send_pref_menu(channel):
    embed = discord.Embed(title="Lobby Preferences", description="*Hey Wanderer, can I lurk on you :>*\n\nSet your lobby preferences from the dropdown below if you are able to play at a particular time shift only.\n\nYou can select a maximum of 3 lobbies at one instance.", color=0x229db7)
    view = LobbyPreferencesView()
    await channel.send(embed=embed, view=view)

class LobbySelectDropdown(discord.ui.Select):
    def __init__(self,disabled = False):
        options = [discord.SelectOption(label=f"Lobby {i}", value=f"{int(i)}", emoji="🌟") for i in range(1, (int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE)) + 1)]
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
            await interaction.response.send_message(f"{user.mention} Current preferences are set to: {', '.join(constants.preferences_dict.get(user.id))}", ephemeral=True,delete_after=60)
        else:
            await interaction.response.send_message(f"{user.mention} Current preferences are set to: None, your chances for confirmed slot are maxmimum!", ephemeral=True,delete_after=60)

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

@bot.command()
@commands.has_permissions(manage_roles=True)
async def setprompt(ctx, *, prompt: str = None):
    try:
        if prompt is None:
            await ctx.send("Invalid prompt. Please provide a non-empty prompt.")
            return

        # Check if the prompt is not an empty string
        if prompt.strip():
            constants.REGISTRATION_PROMPT = prompt
            await ctx.send(f"Registration prompt set to: {prompt}")
        else:
            await ctx.send("Invalid prompt. Please provide a non-empty prompt.")

    except Exception as e:
        await ctx.send(f"An error occurred: {e}")

@bot.command()
@commands.has_permissions(manage_roles=True)
async def start(ctx):
    constants.registered_teams = {}
    try:
        os.remove('registered_teams.csv')
        print("CSV file deleted successfully.")
    except FileNotFoundError:
        await bot.get_channel(constants.MOD_CHANNEL_ID).send("Error: CSV file not found.")
    await unlock_channel(constants.REGISTRATION_CHANNEL_ID)
    await ctx.send("## STARTED")

@bot.event
async def on_message(message):
    # Check if the message is in the desired channel
    if message.channel.id == constants.REGISTRATION_CHANNEL_ID:
        if message.content.strip() == constants.REGISTRATION_PROMPT:
            await confirm(message.author.id,message.id)

        elif message.author.bot:
            return
        
        else:
            await bot.process_commands(message)
        
    else:
        # Process bot commands if the message doesn't match the registration prompt
        await bot.process_commands(message)

async def confirm(user_id, message_id):

    channel = bot.get_channel(constants.REGISTRATION_CHANNEL_ID) 
    if channel:
        try:
            message = await channel.fetch_message(message_id)  # Fetch message from the channel
        except discord.NotFound:
            print("Error: Message not found.")
    else:
        print("Error: Channel not found.")

    # Check if the user is already enrolled
    team_name = validate_registration(user_id)
    if team_name:
        if team_name == 'banned':
            # Send a message to the user with the reason for the ban
            user = bot.get_user(user_id)
            if user:
                await bot.get_channel(constants.SCRIMS_LOG_CHANNEL_ID).send(f"{user.mention} Someone from your team is banned at the moment.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.")
            else:
                print("Error: User not found.")
            await message.add_reaction('❌')

        elif team_name == 'cooldown':
            # Send a message to the user informing about the cooldown
            user = bot.get_user(user_id)
            if user:
                await bot.get_channel(constants.SCRIMS_LOG_CHANNEL_ID).send(f"{user.mention} Someone from your team is on cooldown, please wait for the cooldown period to end\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.")
            else:
                print("Error: User not found.")
            await message.add_reaction('❌')

        elif user_id not in constants.registered_teams:  # Check if the user is not already registered
            if available_slots() > 0:
                print("Available slots:", available_slots())
                await message.add_reaction('✅')
                # Mark registration as confirmed
                await confirm_registration(user_id, team_name)  # Pass team name
                print("Registration confirmed for user:", user_id)
                # Save the registered team's data
                constants.registered_teams[user_id] = team_name
                print("Registered teams' data:", constants.registered_teams)  # Log the registered teams' data
                print("Available slots:", available_slots())
                
                # Assign COOLDOWN_ROLE_ID to the confirmed user
                await assign_role(user_id, constants.COOLDOWN_ROLE_ID)
                
                # Check if all slots are filled
                if available_slots() == 0:

                    await lock_channel(constants.REGISTRATION_CHANNEL_ID)

                    # Create and save the CSV file
                    await saveAsCsv(constants.registered_teams, 'registered_teams.csv')
                    
                    # Send the CSV file to the designated channel
                    channel = bot.get_channel(constants.MOD_CHANNEL_ID)
                    if channel:
                        await channel.send(file=discord.File('registered_teams.csv'))
                    else:
                        print("Error: Designated channel not found.")

                    # Allocate lobby channels
                    await allocate_lobby_channels()

            else:
                print("All slots are filled.")
                await reject_registration(user_id, "Sorry, all slots are filled.")
                await message.add_reaction('❌')
        else:
            print("User is already registered.")
            await reject_registration(user_id, "Your team has already been registered for today.")
            await message.add_reaction('❌')
    else:
        print("User is not enrolled.")
        await reject_registration(user_id, f"Your team is not enrolled yet, please do checkout <#{constants.INFO_CHANNEL_ID}>.")
        await message.add_reaction('❌')

async def enrollTeam(user):

    # Set the flag to indicate that a process is running
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
            if team_name.lower() == "cooldown" or team_name.lower() == "banned":
                await thread.send(f"{user.mention} This team name is not allowed. Please choose a different team name.")

            # Check if the team name already exists
            if not is_team_name_unique(team_name):
                await thread.send(f"{user.mention} This team name already exists.\nYou can modify the team name slightly for it to pass.\nEx: Team Chambal Ke Daku can be written any way like ChambalKeDaku, Chambal Thukai, Chambal ESP, Team Chambal, Chambal Squad. Let's restart your enrollment, my friend!")
            else:
                break  # Exit the loop if a unique team name is provided

        # Get IGNs from players
        player_igns = []
        for i in range(1, 5):
            player_ign = await get_user_response_in_thread(user, thread, f"Please enter Player {i}'s IGN:")
            if not player_ign:
                raise ValueError(f"Player {i}'s IGN cannot be empty.")
            player_igns.append(player_ign)

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
        await asyncio.sleep(int(ee.timeout))  # default = 5 minutes
        await thread.delete()
        pass  # Do nothing, the code execution will stop
    except asyncio.TimeoutError:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Enrollment timed out. Please try again later.")
        await thread.delete()
    except ValueError as ve:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An error occurred during enrollment: {ve}")
        await thread.delete()
    except Exception as e:
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} An unexpected error occurred during enrollment: {e}")
        await thread.delete()

    finally:
        # Reset the flag once the process is finished for this user
        constants.running_processes.pop(user.id, None)

async def create_private_thread(user, name_suffix):
    # Create the private thread with the provided suffix
    thread_name = f"{user.name}-{name_suffix}"
    private_thread = await bot.get_guild(constants.GUILD_ID).get_channel(constants.ENROLLMENT_CHANNEL_ID).create_thread(name=thread_name,invitable=True)

    # Add the user to the private thread
    await private_thread.add_user(user)

    # Return the private thread
    return private_thread

async def updateTeam(user):

    # Set the flag to indicate that a process is running
    constants.running_processes[user.id] = True
    thread = await create_private_thread(user, "update")

    try:
        # Check if the user is already enrolled
        existing_team_message = isAlreadyEnrolled(user.id)
        if existing_team_message:
            await thread.send(existing_team_message)
        else:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} You aren't enrolled in any team as of now. Please select 'Enroll' to create one.")
            await thread.delete()
            return
        
        # Send a message to confirm the user's decision to update their team
        confirmation_message = "Are you sure you want to update your team?\nYou will need to add complete details again of each player if you continue."

        response = await ask_yes_no_question_in_thread(user, thread, confirmation_message)
        if response == 'yes':
            constants.running_processes[user.id] = False
            delete_team_from_sheet(user.id,constants.GOOGLE_SHEET_ID)
            await thread.send("Your previous team data was deleted so even if you are timed out from here, you will need to start enrollment fresh.\n\nLet's Start new enrollment!")
            constants.running_processes.pop(user.id, None)
            await enrollTeam(user)
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
        constants.running_processes.pop(user.id, None)
        
async def deleteTeam(user):

    # Set the flag to indicate that a process is running
    constants.running_processes[user.id] = True
    thread = await create_private_thread(user, "delete")

    try:
        # Check if the user is already enrolled
        existing_team_message = isAlreadyEnrolled(user.id)
        if existing_team_message:
            await thread.send(existing_team_message)
        else:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} You aren't enrolled in any team as of now. Please select 'Enroll' to create one.")
            await thread.delete()
            return
        
        confirmation_message = "Are you sure you want to delete your team?\nIt cant be reverted later on and all details from our end will be lost."

        response = await ask_yes_no_question_in_thread(user, thread, confirmation_message)
        if response == 'yes':
            constants.running_processes[user.id] = False
            delete_team_from_sheet(user.id,constants.GOOGLE_SHEET_ID)
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
        constants.running_processes.pop(user.id, None)

class EnrollmentError(Exception):
    def __init__(self, timeout=300):
        self.timeout = timeout

# async def get_user_response(user, prompt=""):
#     try:
#         await user.send(prompt)
#         response = await bot.wait_for('message', check=lambda msg: msg.author == user and msg.channel.type == discord.ChannelType.private, timeout=120)
#         if response.content.strip():  # Check if the response is not empty after stripping whitespace
#             return response.content
#         else:
#             await user.send("Please provide a non-empty response.")
#             return await get_user_response(user, prompt)  # Ask for response again recursively
#     except asyncio.TimeoutError:
#         await user.send("Response timed out. Please try again later.")
#         return None

async def get_user_response_in_thread(user, channel, prompt="", timeout=120, return_message_object=False):
    await channel.send(prompt)
    response = await bot.wait_for('message', check=lambda msg: msg.author == user and msg.channel == channel, timeout = int(timeout))
    if response.content.strip():  # Check if the response is not empty after stripping whitespace
        if return_message_object:
            return response  # Return the message object if requested
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
            timeout=120
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
    registration_details = "## Team H\n"
    for ign in player_igns:
        registration_details += f"{ign} -\n"

    await thread.send(registration_details)

async def validate_enrollment(user, team_name, player_igns, thread):

    if thread:
        existing_team_message = ""

        # Wait for user response with timeout
        response = await get_user_response_in_thread(user, thread, f"Now fill up the details mentioning players against their IGNs and send it here.\n_Yea you can mention anyone here∆_", 300,True)  # Timeout set to 10 minutes (300 seconds)
        
        if response is None:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} Validation timeout reached. Please reapply.")
            return False

        # Process user response
        mentioned_users = response.mentions
        player_discord_ids = [user.id for user in mentioned_users[:5]]
        
        # Check if any of the mentioned players are already enrolled
        for discord_id in player_discord_ids:
            text = isAlreadyEnrolled(discord_id)
            if text:
                existing_team_message += f"Your enrollment can't proceed as either You or One of your teammate is already a part of some other team:\n"
                existing_team_message += text
                existing_team_message += f"\nIf they're not a part of listed team, reach out to the support team via <#{constants.HELP_CHANNEL_ID}>."
                await thread.send(existing_team_message)
                await response.add_reaction("❌")
                raise EnrollmentError

        # Check if at least 4 users are mentioned
        if len(player_discord_ids) < 4:
            await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} You didn't mention all of your teammates. Please restart the enrollment process and mention correctly next time.")
            await response.add_reaction("❌")
            raise EnrollmentError(60)

        # Check if at least 4 mentioned users have the required role
        for discord_id in player_discord_ids:
            member = bot.get_guild(constants.GUILD_ID).get_member(discord_id)
            if member is None or not any(role.name == constants.REQUIRED_ROLE_NAME for role in member.roles):
                await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"{user.mention} One or more teammates haven't verified on the discord server yet. Reapply once it's done.")
                await response.add_reaction("❌")
                raise EnrollmentError(60)
                    
        # All validation checks passed
        await response.add_reaction("✅")
        await bot.get_channel(constants.TEAM_RECORDS_CHANNEL_ID).send(f"## Enrollment for team {team_name} validated. {user.mention} ")

        # Write enrollment details to Google Sheets
        write_to_sheet(user.id, team_name, player_igns, player_discord_ids)
        await asyncio.sleep(60)
        await thread.delete()          
        return True

    else:
        print("Validation thread not found.")
        return False
    
# Function to write enrollment details to Google Sheets
def write_to_sheet(initiator_id, team_name, player_igns, player_discord_ids):
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

def delete_team_from_sheet(user_id, spreadsheet_id):
    try:
        # Fetch all values from the worksheet
        sheet = constants.service.spreadsheets()
        result = sheet.values().get(spreadsheetId=spreadsheet_id, range="Sheet1").execute()
        values = result.get('values', [])

        # Find the row index containing the user's team
        row_index = None
        for i, row in enumerate(values):
            if str(user_id) in row:
                row_index = i + 1  # Adding 1 to match 1-based index in Sheets API
                break

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

            print("Team data deleted successfully.")
        else:
            print("Team data not found for deletion.")

    except Exception as e:
        print("Error occurred while deleting team data from Google Sheets:", e)

# Function to check if a team name already exists in the Google Sheets
def is_team_name_unique(team_name):
    team_names = constants.sheet.col_values(2)  # Assuming team names are in the first column
    return team_name not in team_names

def isAlreadyEnrolled(user_id):
    try:
        # Fetch all values from the worksheet
        rows = constants.sheet.get_all_values()

        # Iterate through each row to find the user's team
        for row in rows:
            # Check if the user's Discord ID is in the row
            if str(user_id) in row:
                # Extract team details from the row
                team_name = row[1]
                player_igns = row[3::2]  # Player IGNs are in odd indices
                discord_ids = row[2::2]  # Discord IDs are in even indices

                # Construct a message with team details
                message = f"# **Team Name:** {team_name}\n"
                for i, discord_id in enumerate(discord_ids[:4], 1):
                    message += f"{i}. **P{i}**: <@{discord_id}>\n"

                # Check if there's a fifth player's Discord ID
                if len(discord_ids) >= 5 and discord_ids[4].strip():
                    message += f"5. **P5**: <@{discord_ids[4]}>\n"

                return message

        # If the user's team is not found, return None
        return None

    except Exception as e:
        print("Error occurred while checking Discord ID:", e)
        return None

# Function to fetch data from the worksheet and update the cache
def refresh_cache():
    while True:
        try:
            # Fetch all values from the worksheet
            rows = constants.sheet.get_all_values()
            # Update the cached data
            constants.cached_data = rows
            
        except Exception as e:
            print("Error occurred while refreshing cache:", e)
        # Sleep for 60 seconds before refreshing again
        time.sleep(10)

# Start a separate thread to periodically refresh the cache
refresh_thread = threading.Thread(target=refresh_cache)
refresh_thread.daemon = True
refresh_thread.start()

def validate_registration(user_id):
    try:
        global cached_data
        # Use the cached data to validate registration
        if constants.cached_data:
            for row in constants.cached_data:
                if str(user_id) in row:
                    banned_flag = False
                    cooldown_flag = False
                    
                    # Check if any player in the team has a banned or cooldown role
                    for discord_id in row[2::2]: # Discord IDs are in even indices
                        if discord_id and discord_id.isdigit():
                            member = bot.get_guild(constants.GUILD_ID).get_member(int(discord_id))
                            if member:
                                for role in member.roles:
                                    if role.id == constants.BANNED_ROLE_ID:
                                        print(f"Someone from User {user_id} wali team has a banned role.")
                                        banned_flag = True
                                    elif role.id == constants.COOLDOWN_ROLE_ID:
                                        print(f"Someone from User {user_id} wali team is on cooldown.")
                                        cooldown_flag = True
                                    
                    if banned_flag:
                        return 'banned'
                    elif cooldown_flag:
                        return 'cooldown'
                    else:
                        # If no player has a banned or cooldown role, return the team name
                        team_name = row[1]
                        return team_name
        
        # If cached data is not available, fetch fresh data
        else:
            constants.cached_data = constants.sheet.get_all_values()
            for row in constants.cached_data:
                if str(user_id) in row:
                    banned_flag = False
                    cooldown_flag = False
                    
                    # Check if any player in the team has a banned or cooldown role
                    for discord_id in row[2::2]: # Discord IDs are in even indices
                        if discord_id and discord_id.isdigit():
                            member = bot.get_guild(constants.GUILD_ID).get_member(int(discord_id))
                            if member:
                                for role in member.roles:
                                    if role.id == constants.BANNED_ROLE_ID:
                                        print(f"Someone from User {user_id} wali team has a banned role.")
                                        banned_flag = True
                                    elif role.id == constants.COOLDOWN_ROLE_ID:
                                        print(f"Someone from User {user_id} wali team is on cooldown.")
                                        cooldown_flag = True
                                    
                    if banned_flag:
                        return 'banned'
                    elif cooldown_flag:
                        return 'cooldown'
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
def available_slots():
    # Subtract the number of registered teams from the total slots limit
    return constants.SLOTS_LIMIT - len(constants.registered_teams)

async def confirm_registration(user_id, team_name):
    try:
        # Fetch the discord.User object corresponding to the user_id
        user = await bot.fetch_user(user_id)
        
        # Send a message to the user confirming their registration
        await bot.get_channel(constants.SCRIMS_LOG_CHANNEL_ID).send(f"{user.mention} Queek N FINE BRO, Slot for Team {team_name} has been confirmed.")
    except Exception as e:
        print(f"An error occurred while confirming registration: {e}")

# Function to reject the registration
async def reject_registration(user_id, reason):
    try:
        # Fetch the discord.User object corresponding to the user_id
        user = await bot.fetch_user(user_id)
        # Implement your logic to reject the registration
        await bot.get_channel(constants.SCRIMS_LOG_CHANNEL_ID).send(f"{user.mention} {reason}")
    except Exception as e:
        print(f"An error occurred while confirming registration: {e}")

async def saveAsCsv(teams_dict, csv_file):
    # Extract User IDs and team names from the dictionary
    user_ids = [key for key in teams_dict.keys()]
    team_names = [teams_dict[user_id] for user_id in user_ids]
    
    # Create a DataFrame with User IDs and team names as columns
    df = pd.DataFrame({'User_ID': user_ids, 'Team_Name': team_names})
    
    # Write the DataFrame to a CSV file
    df.to_csv(csv_file, index=False)  # Set index=False to exclude row numbers in the CSV file

# Function to assign role to a user
async def assign_role(user_id, role_id):
    try:
        # Assign the role to the member
        await bot.get_guild(constants.GUILD_ID).get_member(user_id).add_roles(bot.get_guild(constants.GUILD_ID).get_role(role_id))
    except Exception as e:
        print(f"An error occurred while assigning role to user {user_id}: {e}")

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
#     # Determine the number of lobbies needed based on the number of registered teams
#     num_teams = len(constants.registered_teams)
#     num_lobbies = (num_teams + 21) // 22  # Round up to the nearest multiple of 22

#     # Iterate over each lobby and allocate channels
#     for lobby_number in range(1, num_lobbies + 1):
#         lobby_role_name = f"Lobby {lobby_number}"
#         lobby_channel_name = f"lobby-{lobby_number}"  # Adjust this according to your channel naming convention

#         guild = bot.get_guild(constants.GUILD_ID) 

#         # Fetch the lobby role and channel
#         lobby_role = discord.utils.get(guild.roles, name=lobby_role_name)
#         lobby_channel = discord.utils.get(guild.channels, name=lobby_channel_name)

#         if lobby_role and lobby_channel:
#             # Calculate the range of teams to allocate to this lobby
#             start_index = (lobby_number - 1) * 22
#             end_index = min(start_index + 22, num_teams)

#             # Extract team names for this lobby
#             team_names = [team_data['team_name'] for _, team_data in list(constants.registered_teams.items())[start_index:end_index]]

#             # Allocate channels to teams in this lobby
#             for index, (user_id, team_name) in enumerate(list(constants.registered_teams.items())[start_index:end_index], start=1):
#                 member = guild.get_member(user_id)
#                 if member:
#                     # Assign lobby role to the team member
#                     await member.add_roles(lobby_role)

#                     # Mention the lobby channel in a message
#                     await bot.get_channel(constants.SCRIMS_LOG_CHANNEL_ID).send(f"{member.mention} Your team has been assigned to {lobby_role_name}. Access your lobby channel here: {lobby_channel.mention}")
#                 else:
#                     print(f"Member with user ID {user_id} not found.")
                
#             # Pass team names and lobby channel to slot list function
#             await send_slots_list(team_names, lobby_channel)
#         else:
#             print(f"Lobby role or channel for Lobby {lobby_number} not found.")

async def allocate_lobby_channels():
    # Initialize a list to store dictionaries for each lobby
    lobby_teams = [{} for _ in range(int(constants.SLOTS_LIMIT / constants.LOBBY_SIZE))]

    # List to hold users whose registrations were not confirmed
    unconfirmed_users = []

    copy_dict = constants.registered_teams.copy()

    # Allocate users with preferences to their preferred lobbies
    for user_id, team_name in copy_dict.items():
        if user_id in constants.preferences_dict:
            preferred_lobbies = constants.preferences_dict[user_id]
            allocated = False

            for lobby_number in preferred_lobbies:
                if len(lobby_teams[lobby_number - 1]) < constants.LOBBY_SIZE:  # Adjusted index
                    lobby_teams[lobby_number - 1][user_id] = team_name  # Adjusted index
                    await assign_team_to_lobby(user_id, team_name, lobby_number)
                    allocated = True
                    break

            del constants.registered_teams[user_id]    

            if not allocated:
                unconfirmed_users.append((user_id, team_name))

    # Notify users whose registrations were not confirmed
    for user_id, team_name in unconfirmed_users:
        await reject_registration(user_id, "We weren't able to find a match for your lobby preference, so your slot was not confirmed.")
        await bot.get_channel(constants.MOD_CHANNEL_ID).send(f"LAFDA MISHAP PARESHANI, yaar {user_id} ki {team_name} ki vajah se ek slot empty rahega for sure, preference f for my rememberance")

    # Make another copy of registered_teams for safe iteration of remaining users
    remaining_teams = constants.registered_teams.copy()

    # Allocate remaining users to available lobbies
    for user_id, team_name in remaining_teams.items():
        for lobby_number, lobby_teams_dict in enumerate(lobby_teams):
            if len(lobby_teams_dict) < constants.LOBBY_SIZE:
                lobby_teams[lobby_number][user_id] = team_name
                await assign_team_to_lobby(user_id, team_name, lobby_number + 1)  # Adjusted index
                del constants.registered_teams[user_id]
                break

    # Check if all allocation processes are completed
    if not constants.registered_teams:
        # Generate CSV files for each lobby
        for lobby_number, lobby_teams_dict in enumerate(lobby_teams, 1):
            csv_file = f"lobby_{lobby_number}_teams.csv"
            await saveAsCsv(lobby_teams_dict, csv_file)
            user_ids = list(lobby_teams_dict.keys())
            team_names = [lobby_teams_dict[user_id] for user_id in user_ids]
            await send_slots_list(team_names, discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=f"lobby-{lobby_number}"))

async def assign_team_to_lobby(user_id, team_name, lobby_number):
    lobby_role_name = f"Lobby {lobby_number}"
    lobby_channel_name = f"lobby-{lobby_number}"

    lobby_role = discord.utils.get(bot.get_guild(constants.GUILD_ID).roles, name=lobby_role_name)
    lobby_channel = discord.utils.get(bot.get_guild(constants.GUILD_ID).channels, name=lobby_channel_name)

    if lobby_role and lobby_channel:
        member = bot.get_guild(constants.GUILD_ID).get_member(user_id)
        if member:
            await member.add_roles(lobby_role)
            await bot.get_channel(constants.SCRIMS_LOG_CHANNEL_ID).send(f"{member.mention} Your team has been assigned to {lobby_role_name}. Access your lobby channel here: {lobby_channel.mention}")
        else:
            print(f"Member with user ID {user_id} not found.")

async def send_slots_list(team_names, lobby_channel):
    # Prepare the slots list message
    slots_list_message = f"# SLOTS LIST:\n\n"
    slots_list_message += "```yaml\n"
    
    # Add the first two slots as "EMPTY" and "RESERVED"
    slots_list_message += f"01. EMPTY\n"
    slots_list_message += f"02. RESERVED\n"

    # Add the team names to the slots list
    for i, team_name in enumerate(team_names, start=3):
        formatted_index = f"{i:02}"  # Ensure two-digit format
        slots_list_message += f"{formatted_index}. {team_name}\n"

    # Add the remaining slots as "RESERVED"
    for i in range(len(team_names) + 2, 26):
        formatted_index = f"{i:02}"  # Ensure two-digit format
        slots_list_message += f"{formatted_index}. RESERVED\n"

    # Close the code block and send the slots list message to the lobby channel
    slots_list_message += f"```\nMake sure to checkout your lobbies schedule here <#{constants.SCHEDULE_CHANNEL_ID}>."
    await lobby_channel.send(slots_list_message)

# Run the bot with the specified token
bot.run(os.environ.get('DISCORD_TOKEN'))
