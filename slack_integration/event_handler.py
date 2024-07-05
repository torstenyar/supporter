import logging
from .message_handler import fetch_message, send_message
from .slack_client import bot_user_id
from utils.azure_data_loader import load_task_data
from utils.azure_openai_client import initialize_client
from utils.supporter import load_log_file
from utils.supporter import load_screenshot
from utils.supporter import determine_point_of_failure
from utils.supporter import load_descr_preceding_steps
from utils.supporter import load_log_preceding_steps
from utils.supporter import generate_error_description
from utils.supporter import perform_cause_analysis
from utils.supporter import extract_data_from_message
from utils.supporter import generate_textual_overview

# Configure logging
logging.basicConfig(level=logging.INFO)

# In-memory dictionary to track reactions per user and message
reaction_tracker = {}

# Define the list of allowed channel IDs
ALLOWED_CHANNELS = ['C07557UUU2K', 'C05D311FKPF', 'C05CFG7D0TU']


def handle_event(data):
    global reaction_tracker  # Ensure we are using the global reaction_tracker
    event = data.get('event', {})
    logging.info("Received event: {}".format(event))
    az_client = initialize_client()

    valid_reactions = ['yara-sup-1', 'yara-sup-backup']

    if event.get('type') == 'reaction_added' and event.get('reaction') in valid_reactions:
        user_id = event.get('user')
        if user_id == bot_user_id:
            logging.info("Skipping event triggered by the bot itself.")
            return

        channel_id = event['item']['channel']
        message_timestamp = event['item']['ts']
        event_timestamp = event['event_ts']

        # Check if the channel is allowed
        if channel_id not in ALLOWED_CHANNELS:
            logging.info("Reaction added in a non-allowed channel. Ignoring the event.")
            return

        logging.info(
            "Handling reaction_added event for channel {} and timestamp {} and event_timestamp {}".format(channel_id,
                                                                                                          message_timestamp,
                                                                                                          event_timestamp))

        # Check if this reaction was already processed for the same user
        if reaction_tracker.get((channel_id, message_timestamp, user_id)):
            logging.info("This reaction has already been processed for this user.")
            return

        # Mark this reaction as processed for this user
        reaction_tracker[(channel_id, message_timestamp, user_id)] = True

        message = fetch_message(channel_id, message_timestamp)
        if message:
            logging.info("Fetched message!")

            # Extract data from the message
            client_name, task_name, prio, run_id = extract_data_from_message(message)
            logging.info("Client Name: {}".format(client_name))
            logging.info("Task Name: {}".format(task_name))
            logging.info("Prio: {}".format(prio))
            logging.info("Run ID: {}".format(run_id))

            # Check if any element is None and send a failure message if so
            if not all([client_name, task_name, run_id]):
                error_message = ":warning: Error: I was unable to extract the necessary information from the message :cry:.\n"
                if not client_name:
                    error_message += "- Client Name could not be found.\n"
                if not task_name:
                    error_message += "- Task Name could not be found.\n"
                if not run_id:
                    error_message += "- Run ID could not be found."

                send_message(channel_id, message_timestamp, error_message, as_text=True)
                logging.error(error_message)
                return

            if '.yrd' in task_name:
                logging.info("This error originates from a user/manually triggered run.")
                error_message = (
                    ":confused: Warning: It looks like this error originates from a user/manual triggered run. "
                    "Currently, I am unable to handle these types of error and am solely focused on cloud orchestrated runs. "
                    "If you believe this is a mistake, please contact support (aka Torsten).")
                send_message(channel_id, message_timestamp, error_message, as_text=True)
                logging.error(error_message)
                return

            # Send initial acknowledgment message
            initial_message = ("Thanks for your request! I will take a moment to analyze the cause of this error. Will "
                               "come back to you ASAP :hourglass_flowing_sand:")
            send_message(channel_id, message_timestamp, initial_message, as_text=True)

            # Load log data and screenshot
            log_file = load_log_file(run_id)
            if log_file is None:
                error_message = ":warning: Error: Unable to fetch the log file. Please try again later."
                send_message(channel_id, message_timestamp, error_message, as_text=True)
                return
            elif log_file == "INVALID_JSON":
                error_message = ":warning: Error: At this moment only JSON formatted log files are supported."
                send_message(channel_id, message_timestamp, error_message, as_text=True)
                return

            screenshot = load_screenshot(run_id)
            if screenshot is None:
                error_message = ":warning: Error: Unable to fetch the screenshot. Please try again later."
                send_message(channel_id, message_timestamp, error_message, as_text=True)
                return
            elif screenshot == "INVALID_IMAGE":
                error_message = ":warning: Error: The screenshot is not a valid image."
                send_message(channel_id, message_timestamp, error_message, as_text=True)
                return

            # Determine point of failure
            point_of_failure_descr, failed_step_id = determine_point_of_failure(log_file)

            # Load the preceding steps
            preceding_steps_log = load_log_preceding_steps(log_file, failed_step_id, steps_to_include=10)

            # Create changes of variables overview
            #variable_changes = generate_textual_overview(log_file, preceding_steps_log)

            process_row, task_data, az_record_found = load_task_data(customer_name=client_name, process_name=task_name)

            process_description = None
            preceding_steps_descr = None

            if az_record_found:
                process_description = process_row['ProcessDescription']
                preceding_steps_descr = load_descr_preceding_steps(preceding_steps_log, task_data)

            error_description = generate_error_description(az_client, client_name, task_name, point_of_failure_descr,
                                                           preceding_steps_log, screenshot)

            cause_analysis = perform_cause_analysis(az_client, client_name, task_name, preceding_steps_log, screenshot,
                                                    error_description)

            # Create the response blocks
            blocks_analysis = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*:memo: _What went wrong?_*"
                    }
                },
                error_description,
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*:mag: _Why did it go wrong? What led me to believe this is the case?_*"
                    }
                },
                cause_analysis
            ]

            # Send the detailed response message using blocks
            send_message(channel_id, message_timestamp, blocks_analysis, as_text=False)

    elif event.get('type') == 'reaction_removed' and event.get('reaction') in valid_reactions:
        user_id = event.get('user')
        if user_id == bot_user_id:
            logging.info("Skipping event triggered by the bot itself.")
            return

        channel_id = event['item']['channel']
        message_timestamp = event['item']['ts']

        logging.info(
            "Handling reaction_removed event for channel {} and timestamp {}".format(channel_id, message_timestamp))

        # Remove the processed mark
        if reaction_tracker.get((channel_id, message_timestamp, user_id)):
            del reaction_tracker[(channel_id, message_timestamp, user_id)]

