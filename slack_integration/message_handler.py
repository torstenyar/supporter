from .slack_client import client
from slack_sdk.errors import SlackApiError
import logging


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
            response = client.chat_postMessage(
                channel=channel,
                blocks=content,
                thread_ts=thread_ts
            )
        logging.info(f"Sent message in thread {thread_ts} in channel {channel}")
    except SlackApiError as e:
        logging.error(f"Error sending message: {e.response['error']}")
