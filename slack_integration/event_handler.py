import json
import logging
from .message_handler import fetch_message, react_to_message, send_message
from .slack_client import bot_user_id
from supporter import (
    load_log_file,
    load_screenshot,
    load_process_description,
    load_preceding_steps,
    determine_point_of_failure,
    generate_error_description,
    perform_cause_analysis,
    suggest_resolution,
    extract_data_from_message
)
import time

# Configure logging
logging.basicConfig(level=logging.INFO)

# In-memory dictionary to track reactions
reaction_tracker = {}


def handle_event(data):
    event = data.get('event', {})
    logging.info(f"Received event: {event}")

    if event.get('type') == 'reaction_added' and event.get('reaction') == 'yara-sup-1':
        user_id = event.get('user')
        if user_id == bot_user_id:
            logging.info("Skipping event triggered by the bot itself.")
            return

        channel_id = event['item']['channel']
        message_timestamp = event['item']['ts']

        logging.info(f"Handling reaction_added event for channel {channel_id} and timestamp {message_timestamp}")

        # Check if this reaction was already processed
        if reaction_tracker.get((channel_id, message_timestamp)):
            logging.info("This reaction has already been processed.")
            return

        # Mark this reaction as processed
        reaction_tracker[(channel_id, message_timestamp)] = True

        message = fetch_message(channel_id, message_timestamp)
        if message:
            logging.info(f"Fetched message!")

            # Extract data from the message
            client_name, task_name, prio, run_id = extract_data_from_message(message)
            logging.info(f"Client Name: {client_name}")
            logging.info(f"Task Name: {task_name}")
            logging.info(f"Prio: {prio}")
            logging.info(f"Run ID: {run_id}")

            # Check if any element is None and send a failure message if so
            if not all([client_name, task_name, prio, run_id]):
                error_message = "Error: I was unable to extract the necessary information from the message :cry:.\n"
                if not client_name:
                    error_message += "- Client Name could not be found.\n"
                if not task_name:
                    error_message += "- Task Name could not be found.\n"
                if not prio:
                    error_message += "- Priority could not be found.\n"
                if not run_id:
                    error_message += "- Run ID could not be found."

                send_message(channel_id, message_timestamp, error_message, as_text=True)
                logging.error(error_message)
                return

            # Send initial acknowledgment message
            initial_message = "Thanks for your request! I will take a moment to analyze the cause of this error. Will come back to you ASAP :hourglass_flowing_sand:"
            send_message(channel_id, message_timestamp, initial_message, as_text=True)

            # Simulate delay for analysis
            time.sleep(5)  # Adjust the delay as needed

            # Load log data and screenshot
            log_file = load_log_file(run_id)
            screenshot = load_screenshot(run_id)

            # Determine point of failure
            point_of_failure = determine_point_of_failure(log_file)

            # Load process description and preceding steps
            process_description = load_process_description()
            recent_steps = load_preceding_steps()

            # Generate content
            error_description = generate_error_description(recent_steps, log_file, process_description, screenshot)
            cause_analysis = perform_cause_analysis(error_description, recent_steps, log_file, process_description,
                                                    screenshot)
            resolution = suggest_resolution(error_description, cause_analysis)

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
                cause_analysis,
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*:hammer_and_wrench: _What steps do you need to take to resolve the issue?_*"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Resolution steps:* To resolve this issue, follow these steps:"
                    }
                },
                resolution,
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Priority:* {prio}"
                    }
                }
            ]

            # Send the detailed response message using blocks
            send_message(channel_id, message_timestamp, blocks_analysis, as_text=False)

    elif event.get('type') == 'reaction_removed' and event.get('reaction') == 'yara-sup-1':
        user_id = event.get('user')
        if user_id == bot_user_id:
            logging.info("Skipping event triggered by the bot itself.")
            return

        channel_id = event['item']['channel']
        message_timestamp = event['item']['ts']

        logging.info(f"Handling reaction_removed event for channel {channel_id} and timestamp {message_timestamp}")

        # Remove the processed mark
        if reaction_tracker.get((channel_id, message_timestamp)):
            del reaction_tracker[(channel_id, message_timestamp)]
