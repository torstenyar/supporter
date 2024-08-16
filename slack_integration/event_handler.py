import logging
import json
import os
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from slack_integration.message_handler import fetch_message, send_message
from slack_integration.slack_client import get_bot_user_id
from utils.azure_openai_client import initialize_client
from utils.supporter import (
    load_log_file, load_screenshot, determine_point_of_failure,
    load_log_preceding_steps, generate_error_description,
    perform_cause_analysis, extract_data_from_message
)

# Uncomment below for local testing
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)

# In-memory dictionary to track reactions per user, message, and emoji
reaction_tracker = {}

# Define the list of allowed channel IDs and valid reactions based on environment
CHANNEL_CONFIG = {
    'development': ['C07557UUU2K'],  # Test channel for development app
    'production': ['C07557UUU2K', 'C05D311FKPF', 'C05CFG7D0TU']  # Production channels
}

REACTION_CONFIG = {
    'development': ['test_tube'],  # Test reaction for development app
    'production': ['yara-sup-1', 'yara-sup-backup']  # Production reactions
}

# Azure Service Bus configuration (same for both environments)
SERVICEBUS_CONNECTION_STR = os.environ['SERVICEBUS_CONNECTION_STR']
SUPPORTER_DATA_QUEUE = os.environ['SUPPORTER_DATA_QUEUE']
SUPPORTER_TRIGGERED = os.environ['SUPPORTER_TRIGGERED']


def send_supporter_data_to_uardi(data):
    """
    Sends the given data to the SUPPORTER_DATA_QUEUE in Azure Service Bus.
    """
    try:
        servicebus_client = ServiceBusClient.from_connection_string(conn_str=SERVICEBUS_CONNECTION_STR)
        with servicebus_client:
            sender = servicebus_client.get_queue_sender(queue_name=SUPPORTER_DATA_QUEUE)
            with sender:
                message = ServiceBusMessage(json.dumps(data))
                sender.send_messages(message)
                logging.info("Sent message to SUPPORTER_DATA_QUEUE")
    except Exception as e:
        logging.error(f"Failed to send message to queue: {e}")


def send_task_run_id_to_yarado(data):
    """
    Sends the given data to the SUPPORTER_TRIGGERED in Azure Service Bus.
    """
    try:
        servicebus_client = ServiceBusClient.from_connection_string(conn_str=SERVICEBUS_CONNECTION_STR)
        with servicebus_client:
            sender = servicebus_client.get_queue_sender(queue_name=SUPPORTER_TRIGGERED)
            with sender:
                message = ServiceBusMessage(json.dumps(data))
                sender.send_messages(message)
                logging.info("Sent task_run_id to SUPPORTER_TRIGGERED")
    except Exception as e:
        logging.error(f"Failed to send message to queue: {e}")


def handle_event(data, environment, slack_client):
    global reaction_tracker
    event = data.get('event', {})
    logging.info(f"Received event in {environment} environment: {event}")

    bot_user_id = get_bot_user_id(slack_client)
    az_client = initialize_client()

    allowed_channels = CHANNEL_CONFIG[environment]
    valid_reactions = REACTION_CONFIG[environment]

    if event.get('type') == 'reaction_added' and event.get('reaction') in valid_reactions:
        user_id = event.get('user')
        reaction = event.get('reaction')
        if user_id == bot_user_id:
            logging.info("Skipping event triggered by the bot itself.")
            return

        channel_id = event['item']['channel']
        message_timestamp = event['item']['ts']
        event_timestamp = event['event_ts']

        if channel_id not in allowed_channels:
            logging.info("Reaction added in a non-allowed channel. Ignoring the event.")
            return

        logging.info(
            f"Handling reaction_added event for channel {channel_id} and timestamp {message_timestamp} and event_timestamp {event_timestamp}")

        if reaction_tracker.get((channel_id, message_timestamp, user_id, reaction)):
            logging.info("This reaction has already been processed for this user and emoji.")
            return

        reaction_tracker[(channel_id, message_timestamp, user_id, reaction)] = True

        message = fetch_message(slack_client, channel_id, message_timestamp)
        if message:
            logging.info("Fetched message!")

            client_name, task_name, prio, run_id = extract_data_from_message(message)
            logging.info(f"Client Name: {client_name}")
            logging.info(f"Task Name: {task_name}")
            logging.info(f"Prio: {prio}")
            logging.info(f"Run ID: {run_id}")

            if not all([client_name, task_name, run_id]):
                error_message = ":warning: Error: I was unable to extract the necessary information from the message :cry:.\n"
                if not client_name:
                    error_message += "- Client Name could not be found.\n"
                if not task_name:
                    error_message += "- Task Name could not be found.\n"
                if not run_id:
                    error_message += "- Run ID could not be found."

                send_message(slack_client, channel_id, message_timestamp, error_message, as_text=True)
                logging.error(error_message)
                return

            if '.yrd' in task_name:
                error_message = (
                    ":confused: Warning: It looks like this error originates from a user/manual triggered run. "
                    "Currently, I am unable to handle these types of error and am solely focused on cloud orchestrated runs. "
                    "If you believe this is a mistake, please contact support (aka Torsten).")
                send_message(slack_client, channel_id, message_timestamp, error_message, as_text=True)
                logging.error(error_message)
                return

            initial_message = ("Thanks for your request! I will take a moment to analyze the cause of this error. Will "
                               "come back to you ASAP :hourglass_flowing_sand:")
            send_message(slack_client, channel_id, message_timestamp, initial_message, as_text=True)

            log_file = load_log_file(run_id)
            if log_file is None:
                error_message = ":warning: Error: Unable to fetch the log file. Please try again later."
                send_message(slack_client, channel_id, message_timestamp, error_message, as_text=True)
                return
            elif log_file == "INVALID_JSON":
                error_message = ":warning: Error: At this moment only JSON formatted log files are supported."
                send_message(slack_client, channel_id, message_timestamp, error_message, as_text=True)
                return

            screenshot = load_screenshot(run_id)
            if screenshot is None:
                error_message = ":warning: Error: Unable to fetch the screenshot. Please try again later."
                send_message(slack_client, channel_id, message_timestamp, error_message, as_text=True)
                return
            elif screenshot == "INVALID_IMAGE":
                error_message = ":warning: Error: The screenshot is not a valid image."
                send_message(slack_client, channel_id, message_timestamp, error_message, as_text=True)
                return

            point_of_failure_descr, failed_step_id = determine_point_of_failure(log_file)
            preceding_steps_log = load_log_preceding_steps(log_file, failed_step_id, steps_to_include=10)

            retry_count = 0
            max_retries = 3
            while retry_count < max_retries:
                try:
                    error_description = generate_error_description(az_client, client_name, task_name,
                                                                   point_of_failure_descr,
                                                                   preceding_steps_log, screenshot)
                    cause_analysis = perform_cause_analysis(az_client, client_name, task_name, preceding_steps_log,
                                                            screenshot,
                                                            error_description)

                    blocks_analysis = json.dumps([
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "*:memo: _What went wrong?_*"
                            }
                        },
                        json.loads(error_description),
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "*:mag: _Why did it go wrong? What led me to believe this is the case?_*"
                            }
                        },
                        json.loads(cause_analysis)
                    ])

                    try:
                        send_message(slack_client, channel_id, message_timestamp, blocks_analysis, as_text=False)
                    except Exception as e:
                        logging.error(f"Error sending message: {str(e)}")
                        logging.error(f"Attempted to send blocks: {blocks_analysis}")

                    supporter_data = {
                        "task_run_id": run_id,
                        "task_name": task_name,
                        "organisation_name": client_name,
                        "step_id_pof": failed_step_id,
                        "ai_cause": json.loads(cause_analysis)['text']['text'],
                        "ai_description": json.loads(error_description)['text']['text']
                    }

                    yarado_data = {
                        "task_run_id": run_id
                    }

                    if environment == 'production':  # Send data only in production mode
                        send_supporter_data_to_uardi(supporter_data)
                        send_task_run_id_to_yarado(yarado_data)

                    break

                except Exception as e:
                    logging.error(f"Failed to send message: {e}. Retry {retry_count + 1}/{max_retries}")
                    retry_count += 1
                    if retry_count == max_retries:
                        error_message = (":warning: Error: Unable to send the analysis message after multiple "
                                         "attempts. Please try again later.")
                        send_message(slack_client, channel_id, message_timestamp, error_message, as_text=True)
                        return

    elif event.get('type') == 'reaction_removed' and event.get('reaction') in valid_reactions:
        user_id = event.get('user')
        reaction = event.get('reaction')
        if user_id == bot_user_id:
            logging.info("Skipping event triggered by the bot itself.")
            return

        channel_id = event['item']['channel']
        message_timestamp = event['item']['ts']

        logging.info(f"Handling reaction_removed event for channel {channel_id} and timestamp {message_timestamp}")

        if reaction_tracker.get((channel_id, message_timestamp, user_id, reaction)):
            del reaction_tracker[(channel_id, message_timestamp, user_id, reaction)]
