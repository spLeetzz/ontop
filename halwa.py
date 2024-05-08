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
    global service
    global sheet

    # Get the JSON key file path from an environment variable
    json_keyfile_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "default_path")
    
    # Check if the environment variable is set and use it, otherwise use a default path
    if json_keyfile_path == "default_path":
        # If the environment variable is not set, use a default path
        json_keyfile_path = "D:/Google Cloud JSON Key/clear-healer-415920-7abc2cda379e.json"
        
    global INTERACTION_MESSAGE_ID
    INTERACTION_MESSAGE_ID = constants.INTERACTION_MESSAGE_ID

    try:
        # Attempt to connect to Google Sheets
        sheet, service = connect_to_google_sheets(json_keyfile_path)
    except Exception as e:
        print("Error while connecting to Google Sheets:", e)
    
    print(f'We have logged in as {bot.user}')
    
    # Fetch the channel
    channel = bot.get_channel(constants.ENROLLMENT_CHANNEL_ID)
    # Check if the interaction message ID is stored and the channel is valid
    if INTERACTION_MESSAGE_ID and channel:
        try:
            # Fetch the message
            message = await channel.fetch_message(INTERACTION_MESSAGE_ID)
        except discord.NotFound:
            # If the message is not found, reset the interaction message ID
            INTERACTION_MESSAGE_ID = None
            return
        # Check if the message exists
        if message:
            # Edit the message with the dropdown menu
            await message.edit(content="Select an action:", view=TournamentView())
        else:
            # Send the initial message with the dropdown menu
            message = await send_selectmenu(channel)
            # Store the interaction message ID
            INTERACTION_MESSAGE_ID = message.id
    else:
        # If the channel or message ID is not valid, send the select menu
        await send_selectmenu(channel)

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
        if selected_value == "enroll":
            await interaction.response.send_message("Enrollment Started, Check your DM!",ephemeral=True, delete_after=30)
            await enrollTeam(user)
            await interaction.message.edit(view=TournamentView())
        elif selected_value == "update":
            await interaction.response.send_message("Update selected, Check your DM",ephemeral=True, delete_after=30)
            await updateTeam(user)
            await interaction.message.edit(view=TournamentView())
        elif selected_value == "delete":
            await interaction.response.send_message("Delete selected, Check your DM!",ephemeral=True, delete_after=30)
            await deleteTeam(user)
            await interaction.message.edit(view=TournamentView())

# Define the TournamentView class inheriting from discord.ui.View
class TournamentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Set timeout to None to make the view persistent
        self.add_item(TournamentDropdown())

async def send_selectmenu(channel):
    embed = discord.Embed(title="Team Enrollment", description="Select appropriate option to CREATE/UPDATE/DELETE your team from the dropdown below", color=0x007CFF)
    select = TournamentDropdown()

    view = TournamentView()  # Initialize TournamentView with timeout=None
    view.add_item(select)

    await channel.send(embed=embed, view=view)

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
    team_info = validate_registration(user_id)
    if team_info:
        if user_id not in constants.registered_teams:  # Check if the user is not already registered
            if team_info == 'banned':
                # Send a DM to the user with the reason for the ban
                user = bot.get_user(user_id)
                if user:
                    await user.send(f"Someone from your team is banned at the moment.\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.")
                else:
                    print("Error: User not found.")
                await message.add_reaction('❌')

            elif team_info == 'cooldown':
                # Send a DM to the user informing about the cooldown
                user = bot.get_user(user_id)
                if user:
                    await user.send(f"Someone from your team is on cooldown, please wait for the cooldown period to end\nReach out to the support team in case there's an issue via <#{constants.HELP_CHANNEL_ID}>.")
                else:
                    print("Error: User not found.")
                await message.add_reaction('❌')

            elif available_slots() > 0:
                print("Available slots:", available_slots())
                # Mark registration as confirmed
                await confirm_registration(user_id, team_info['team_name'])  # Pass team name
                print("Registration confirmed for user:", user_id)
                # Save the registered team's data
                constants.registered_teams[user_id] = team_info['team_name']
                print("Registered teams' data:", constants.registered_teams)  # Log the registered teams' data
                print("Available slots:", available_slots())
                await message.add_reaction('✅')
                
                # Check if all slots are filled
                if available_slots() == 0:

                    # After sending the message with the CSV file and all slots are filled
                    # Call the assign_role function for each confirmed user
                    for user_id in constants.registered_teams.keys():
                        await assign_role(user_id, constants.COOLDOWN_ROLE_ID)

                    # Create and save the CSV file
                    saveAsCsv(constants.registered_teams, 'registered_teams.csv')
                    
                    # Send the CSV file to the designated channel
                    channel = bot.get_channel(constants.MOD_CHANNEL_ID)
                    if channel:
                        await channel.send(file=discord.File('registered_teams.csv'))
                    else:
                        print("Error: Designated channel not found.")
                    
                    await lock_channel(constants.REGISTRATION_CHANNEL_ID)

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

    # If a process is already running, ignore the request
    if constants.running_processes.get(user.id):
        await user.send("Another process is already running.\nWait until previous process is timed out (that'll take upto 2 mins of inactivity)")
        return

    # Set the flag to indicate that a process is running
    constants.running_processes[user.id] = True

    try:
        # Check if the user is already enrolled
        team_details = isAlreadyEnrolled(user.id)

        if team_details:
            existing_team_message = f"Your enrollment can't proceed as either You or One of your teammate is already a part of some other team:\n"
            existing_team_message += team_details
            existing_team_message += f"\nIf they're not a part of listed team, reach out to the support team via <#{constants.HELP_CHANNEL_ID}>."
            await user.send(existing_team_message)
            raise EnrollmentCompleteError

        # Loop until a unique team name is provided
        while True:
            # Get the team name from the user
            team_name = await get_user_response(user, "Please enter your team name:")

            # Check if the team name is empty
            if not team_name or team_name == None:
                raise ValueError("Team name cannot be empty.")

            # Check if the team name already exists
            if not is_team_name_unique(team_name):
                await user.send("This team name already exists.\nYou can modify team name slightly for it to pass.\nEx: Team Chamabal Ke Daku can be written any way like ChambalKeDaku, Chambal Thukai, Chambal ESP, Team Chambal, Chambal Squad. Lets restart your enrollment my friend!")
            else:
                break  # Exit the loop if a unique team name is provided

        # # Check if team_name starts with "Team " and remove it
        # if team_name.lower().startswith("team "):
        #     team_name = team_name[5:]

        player1_ign = await get_user_response(user, "Please enter Player 1's IGN:")
        if not player1_ign:
            raise ValueError("Player 1's IGN cannot be empty.")

        player2_ign = await get_user_response(user, "Please enter Player 2's IGN:")
        if not player2_ign:
            raise ValueError("Player 2's IGN cannot be empty.")

        player3_ign = await get_user_response(user, "Please enter Player 3's IGN:")
        if not player3_ign:
            raise ValueError("Player 3's IGN cannot be empty.")

        player4_ign = await get_user_response(user, "Please enter Player 4's IGN:")
        if not player4_ign:
            raise ValueError("Player 4's IGN cannot be empty.")

        # Ask if there is a fifth player
        fifth_player_response = await ask_yes_no_question(user, "Do you have a fifth player?")
        player5_ign = None
        if fifth_player_response == 'yes':
            player5_ign = await get_user_response(user, "Please enter Player 5's IGN:")
            if not player5_ign:
                raise ValueError("Player 5's IGN cannot be empty.")

        # Write registration details to Google Sheets
        player_igns = [player1_ign, player2_ign, player3_ign, player4_ign]
        if player5_ign:
            player_igns.append(player5_ign)

        await send_registration_details(user, team_name, player_igns)
        await validate_enrollment(user, team_name, player_igns)

    except EnrollmentCompleteError:
        pass  # Do nothing, the code execution will stop  
    except asyncio.TimeoutError:
        await user.send("Enrollment timed out. Please try again later.")
    except ValueError as ve:
        await user.send(f"An error occurred during enrollemnt: {ve}")
    except Exception as e:
        await user.send(f"An unexpected error occurred during enrollment: {e}")

    finally:
        # Reset the flag once the process is finished for this user
        constants.running_processes.pop(user.id, None)

async def updateTeam(user):
    
    # If a process is already running, ignore the request
    if constants.running_processes.get(user.id):
        await user.send("Another process is already running.\nWait until previous process is timed out (that'll take upto 2 mins of inactivity)")
        return

    # Set the flag to indicate that a process is running
    constants.running_processes[user.id] = True

    try:
        # Check if the user is already enrolled
        existing_team_message = isAlreadyEnrolled(user.id)
        if existing_team_message:
            await user.send(existing_team_message)
        else:
            await user.send("You aren't enrolled in any team as of now. Please select 'Enroll' to create one.")
            return
        
        # Send a message to confirm the user's decision to update their team
        confirmation_message = "Are you sure you want to update your team?\nYou will need to add complete details again of each player if you continue."

        response = await ask_yes_no_question(user, confirmation_message)
        if response == 'yes':
            constants.running_processes = False
            delete_team_from_sheet(user.id,constants.GOOGLE_SHEET_ID)
            await user.send("Your previous team data was deleted so even if you are timed out from here, you will need to start enrollment fresh.\n\nLet's Start new enrollment!")
            constants.running_processes.pop(user.id, None)
            await enrollTeam(user)
        else:
            await user.send("Update cancelled.")
            return
 
    except asyncio.TimeoutError:
        await user.send("Update timed out. Please try again later.")
    except ValueError as ve:
        await user.send(f"An error occurred during update: {ve}")
    except Exception as e:
        await user.send(f"An unexpected error occurred during update: {e}")
        
    finally:
        # Reset the flag once the process is finished for this user
        constants.running_processes.pop(user.id, None)
        
async def deleteTeam(user):
    
    # If a process is already running, ignore the request
    if constants.running_processes.get(user.id):
        await user.send("Another process is already running.\nWait until previous process is timed out (that'll take upto 2 mins of inactivity)")
        return

    # Set the flag to indicate that a process is running
    constants.running_processes[user.id] = True

    try:
        # Check if the user is already enrolled
        existing_team_message = isAlreadyEnrolled(user.id)
        if existing_team_message:
            await user.send(existing_team_message)
        else:
            await user.send("You aren't enrolled in any team as of now. Please select 'Enroll' to create one.")
            return
        
        confirmation_message = "Are you sure you want to delete your team? (yes/no)\nIt cant be reverted."

        response = await ask_yes_no_question(user, confirmation_message)
        if response == 'yes':
            constants.running_processes = False
            delete_team_from_sheet(user.id,constants.GOOGLE_SHEET_ID)
            await user.send("Deleted successfully")
            return
        else:
            await user.send("Delete cancelled.")
            return
        
    except asyncio.TimeoutError:
        await user.send("Delete timed out. Please try again later.")
    except ValueError as ve:
        await user.send(f"An error occurred during delete: {ve}")
    except Exception as e:
        await user.send(f"An unexpected error occurred during delete: {e}")
 
    finally:
        # Reset the flag once the process is finished for this user
        constants.running_processes.pop(user.id, None)

class EnrollmentCompleteError(Exception):
    pass

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

async def ask_yes_no_question(user, question):
    try:
        # Ask the user the question
        await user.send(question + " (yes/no)")

        # Wait for the user's response with a timeout of 2 minutes (120 seconds)
        response = await asyncio.wait_for(
            bot.wait_for('message', check=lambda msg: msg.author == user and msg.channel.type == discord.ChannelType.private),
            timeout=120
        )

        # Get the content of the response and convert it to lowercase
        response = response.content.lower()

        # Check if the response is either 'yes' or 'no'
        if response in ['yes', 'no']:
            return response
        else:
            await user.send("Enter either 'yes' or 'no'.")
            # Recursively call the function to ask the question again
            return await ask_yes_no_question(user, question)
            
    except asyncio.TimeoutError:
        # Handle the case when the timeout occurs
        await user.send("Response timed out. Please try again later.")
        return None

async def send_registration_details(user, team_name, player_igns):
    registration_details = f"## Copy the meta data beneath\n\n```Team {team_name}\n\n"
    for ign in player_igns:
        registration_details += f"{ign} -\n"
    registration_details += "```"

    await user.send(registration_details)
    await user.send(f"\nValidate your details by mentioning players against their IGNs in <#{constants.VALIDATION_CHANNEL_ID}>.")

async def validate_enrollment(user, team_name, player_igns):
    validation_channel = bot.get_channel(constants.VALIDATION_CHANNEL_ID)
    if validation_channel:
        existing_team_message = ""
        await validation_channel.send(f"Please validate the enrollment for team {team_name} by mentioning all your teammates in this channel. {user.mention}")

        start_time = datetime.datetime.now()
        timeout_duration = datetime.timedelta(minutes=10)  # Set a timeout duration of 10 minutes

        while datetime.datetime.now() - start_time < timeout_duration:
            async for message in validation_channel.history(limit=None, after=start_time):
                if message.author == user:
                    guild = bot.get_guild(constants.GUILD_ID)  # Replace GUILD_ID with your actual guild ID
                    member = guild.get_member(user.id)  # Get the Member object

                    if member is None:
                        await user.send("Failed to retrieve your member information. Please try again later or contact the server administrator.")
                        return False

                    mentioned_users = message.mentions

                    # Assign mentioned users to new collection
                    player_discord_ids = [user.id for user in mentioned_users[:5]]

                    # Check if any of the mentioned players are already enrolled
                    for discord_id in player_discord_ids:
                        text = isAlreadyEnrolled(discord_id)

                        if text:
                            existing_team_message += f"Your enrollment can't proceed as either You or One of your teammate is already a part of some other team:\n"
                            existing_team_message += text
                            existing_team_message += f"\nIf they're not a part of listed team, reach out to the support team via <#{constants.HELP_CHANNEL_ID}>."

                            await user.send(existing_team_message)
                            await message.add_reaction("❌")
                            raise EnrollmentCompleteError

                    # Check if at least 4 users are mentioned
                    if len(player_discord_ids) < 4:
                        await user.send("You didn't mentioned all of your teammates. Please restart the enrollment process and mention correctly next time.")
                        await message.add_reaction("❌")
                        return False

                    # Check if at least 4 mentioned users have the required role
                    for mentioned_user in mentioned_users[:4]:
                        member = guild.get_member(mentioned_user.id)
                        if member is None or not any(role.name == constants.REQUIRED_ROLE_NAME for role in member.roles):
                            await user.send("One or more teammates haven't verified on the discord server yet. Reapply once it's done.")
                            await message.add_reaction("❌")
                            return False
                
                    # All validation checks passed
                    await message.add_reaction("✅")
                    await user.send(f"## Enrollment for team {team_name} validated.")

                    # Write enrollment details to Google Sheets
                    write_to_sheet(user.id, team_name, player_igns, player_discord_ids)
                    
                    return True

        # Timeout reached, inform the user
        await user.send("Validation timeout reached. Please reapply.")
        return False
    else:
        print("Validation channel not found.")
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
    sheet.append_row(row)
    
    # Print registration details for verification
    print(f"Registered: {initiator_idstr}, Team: {team_name}, Discord Usernames: {', '.join(player_discord_ids)}, IGNs: {', '.join(player_igns)}")

def delete_team_from_sheet(user_id, spreadsheet_id):
    try:
        # Fetch all values from the worksheet
        sheet = service.spreadsheets()
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
    team_names = sheet.col_values(2)  # Assuming team names are in the first column
    return team_name not in team_names

def isAlreadyEnrolled(user_id):
    try:
        # Fetch all values from the worksheet
        rows = sheet.get_all_values()

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
            rows = sheet.get_all_values()
            # Update the cached data
            constants.cached_data = rows
            print("cached_data refreshed!")
            
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
                        return {'team_name': team_name}
        
        # If cached data is not available, fetch fresh data
        else:
            constants.cached_data = sheet.get_all_values()
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
                        return {'team_name': team_name}
        
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
        await user.send("Your registration has been confirmed.")
    except Exception as e:
        print(f"An error occurred while confirming registration: {e}")

# Function to reject the registration
async def reject_registration(user_id, reason):
    try:
        # Fetch the discord.User object corresponding to the user_id
        user = await bot.fetch_user(user_id)
        # Implement your logic to reject the registration
        await user.send(reason)
    except Exception as e:
        print(f"An error occurred while confirming registration: {e}")

def saveAsCsv(teams_data, csv_file):
    # Extract User IDs and team names from the dictionary
    user_ids = list(teams_data.keys())
    team_names = [teams_data[user_id]['team_name'] for user_id in user_ids]
    
    # Create a DataFrame with User IDs and team names as columns
    df = pd.DataFrame({'User_ID': user_ids, 'Team_Name': team_names})
    
    # Write the DataFrame to a CSV file
    df.to_csv(csv_file, index=False)  # Set index=False to exclude row numbers in the CSV file

# Function to assign role to a user
async def assign_role(user_id, role_id):
    try:
        # Fetch the guild object
        guild = bot.get_guild(constants.GUILD_ID)  # Replace GUILD_ID with your actual guild ID

        # Fetch the member object corresponding to the user ID
        member = guild.get_member(user_id)

        # Fetch the role object corresponding to the role ID
        role = guild.get_role(role_id)

        # Assign the role to the member
        await member.add_roles(role)
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

async def allocate_lobby_channels():
    # Determine the number of lobbies needed based on the number of registered teams
    num_teams = len(constants.registered_teams)
    num_lobbies = (num_teams + 21) // 22  # Round up to the nearest multiple of 22

    # Iterate over each lobby and allocate channels
    for lobby_number in range(1, num_lobbies + 1):
        lobby_role_name = f"Lobby {lobby_number}"
        lobby_channel_name = f"lobby-{lobby_number}"  # Adjust this according to your channel naming convention

        guild = bot.get_guild(constants.GUILD_ID) 

        # Fetch the lobby role and channel
        lobby_role = discord.utils.get(guild.roles, name=lobby_role_name)
        lobby_channel = discord.utils.get(guild.channels, name=lobby_channel_name)

        if lobby_role and lobby_channel:
            # Calculate the range of teams to allocate to this lobby
            start_index = (lobby_number - 1) * 22
            end_index = min(start_index + 22, num_teams)

            # Extract team names for this lobby
            team_names = [team_data['team_name'] for _, team_data in list(constants.registered_teams.items())[start_index:end_index]]

            # Allocate channels to teams in this lobby
            for index, (user_id, team_name) in enumerate(list(constants.registered_teams.items())[start_index:end_index], start=1):
                member = guild.get_member(user_id)
                if member:
                    # Assign lobby role to the team member
                    await member.add_roles(lobby_role)

                    # Mention the lobby channel in a message
                    await member.send(f"Your team has been assigned to {lobby_role_name}. Access your lobby channel here: {lobby_channel.mention}")
                else:
                    print(f"Member with user ID {user_id} not found.")
                
            # Pass team names and lobby channel to slot list function
            await send_slots_list(team_names, lobby_channel)
        else:
            print(f"Lobby role or channel for Lobby {lobby_number} not found.")

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
