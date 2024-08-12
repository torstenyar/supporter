from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def initialize_slack_client(token):
    return WebClient(token=token)


def get_bot_user_id(client):
    try:
        response = client.auth_test()
        bot_user_id = response["user_id"]
        return bot_user_id
    except SlackApiError as e:
        print(f"Error getting bot user ID: {e.response['error']}")
        return None
