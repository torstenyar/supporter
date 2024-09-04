from slack_sdk.errors import SlackApiError
from slack_sdk import WebClient
import logging
import json


def fetch_message(client, channel, timestamp):
    try:
        response = client.conversations_history(
            channel=channel,
            latest=timestamp,
            inclusive=True,
            limit=1
        )
        message = response['messages'][0]
        return message
    except SlackApiError as e:
        logging.error(f"Error fetching message: {e.response['error']}")
        return None


def react_to_message(client, channel, timestamp, reaction):
    try:
        response = client.reactions_add(
            channel=channel,
            timestamp=timestamp,
            name=reaction
        )
        logging.info(f"Added reaction {reaction} to message {timestamp} in channel {channel}")
    except SlackApiError as e:
        logging.error(f"Error adding reaction: {e.response['error']}")


def send_message(client, channel, thread_ts, content, as_text=True, fallback_content=""):
    blocks=None
    try:
        if as_text:
            response = client.chat_postMessage(
                channel=channel,
                text=content,
                thread_ts=thread_ts
            )
            logging.info(f"Sent message in thread {thread_ts} in channel {channel}")
            return response
        else:
            # Ensure `content` is treated as blocks if it is a list
            blocks = content if isinstance(content, list) else json.loads(content)
            response = client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                thread_ts=thread_ts,
                text="Analysis results (please view in Slack for formatted content)"  # Fallback text
            )
            logging.info(f"Sent message in thread {thread_ts} in channel {channel}")
            return response
    except json.JSONDecodeError as e:
        logging.error(f"Error parsing JSON content: {e}")
    except SlackApiError as e:
        logging.error(f"Error sending message: {e.response['error']}")
        if e.response['error'] == "invalid_blocks":
            logging.error(f'Invalid blocks were: {json.dumps(blocks, indent=2)}')
            # Send the summary with an apology if blocks are invalid
            apology_message = (
                f":warning: Apologies, the detailed analysis could not be formatted correctly.\n"
                f"Here is a brief summary:\n\n{fallback_content}"
            )
            try:
                response = client.chat_postMessage(
                    channel=channel,
                    text=apology_message,
                    thread_ts=thread_ts
                )
                logging.info("Sent fallback message with summary due to invalid blocks.")
            except SlackApiError as e2:
                logging.error(f"Error sending fallback message: {e2.response['error']}")


def update_progress(slack_client, channel_id, message_timestamp, percentage, thread_ts, stage, attempt=None,
                    max_retries=3):
    stages = {
        "fetch_data": "Fetching data faster than a squirrel collecting nuts! 🐿️",
        "analyze_logs": "Diving deep into the logs like a digital detective... 🕵️‍♂️",
        "context_generation": "Putting the pieces together like a puzzle master... 🧩",
        "error_description": "Crafting a story from the data... 📚",
        "cause_analysis": "Putting on my thinking cap to figure out what went wrong... 🤔",
        "solution_generation": "Brainstorming solutions like a caffeinated engineer! ☕️",
        "final_analysis": "Polishing the results to make them shine... ✨",
        "retrying_block_formatting": "Reformatting the analysis... retry in progress 🔄",
        "retrying_message_sending": "Retrying message sending... 🔁"
    }

    # If this is a retry attempt, modify the message accordingly
    if "retrying" in stage and attempt:
        progress_message = f"{stages.get(stage.split('_')[0], 'Retrying...')} (Attempt {attempt}/{max_retries})"
    else:
        progress_message = f"{stages.get(stage, 'Working hard...')} ({percentage}% complete)"

    try:
        slack_client.chat_update(
            channel=channel_id,
            ts=message_timestamp,
            thread_ts=thread_ts,
            text=progress_message,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Analysis Progress*\n{progress_message}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": generate_progress_bar(percentage)
                    }
                }
            ]
        )
    except SlackApiError as e:
        logging.error(f"Error updating progress: {e}")


def generate_progress_bar(percentage: int) -> str:
    """
    Generates a textual representation of a progress bar with colored blocks.
    The progress bar consists of 20 blocks, where each block represents 5% progress.
    Uses emojis to simulate color.
    """
    total_blocks = 20
    filled_blocks = int((percentage / 100) * total_blocks)
    empty_blocks = total_blocks - filled_blocks

    # Use emojis to represent progress
    filled_block = "🟩"  # Green block for completed part
    empty_block = "⬜"  # White block for incomplete part

    progress_bar = f"[{filled_block * filled_blocks}{empty_block * empty_blocks}] {percentage}%"
    return progress_bar

