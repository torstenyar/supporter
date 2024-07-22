from .slack_client import client
from slack_sdk.errors import SlackApiError
import logging
import json


def fetch_message(channel, timestamp):
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


def react_to_message(channel, timestamp, reaction):
    try:
        response = client.reactions_add(
            channel=channel,
            timestamp=timestamp,
            name=reaction
        )
        logging.info(f"Added reaction {reaction} to message {timestamp} in channel {channel}")
    except SlackApiError as e:
        logging.error(f"Error adding reaction: {e.response['error']}")


def send_message(channel, thread_ts, content, as_text=True):
    try:
        if as_text:
            response = client.chat_postMessage(
                channel=channel,
                text=content,
                thread_ts=thread_ts
            )
        else:
            # Parse the JSON string into a Python dictionary
            blocks = json.loads(content)
            # Ensure we're sending a list of blocks
            if not isinstance(blocks, list):
                blocks = [blocks]
            response = client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                thread_ts=thread_ts,
                text="Analysis results (please view in Slack for formatted content)"  # Fallback text
            )
        logging.info(f"Sent message in thread {thread_ts} in channel {channel}")
    except json.JSONDecodeError as e:
        logging.error(f"Error parsing JSON content: {e}")
    except SlackApiError as e:
        logging.error(f"Error sending message: {e.response['error']}")
