import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Initialize a Slack client with the token from environment variables
client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))


def get_bot_user_id():
    try:
        response = client.auth_test()
        bot_user_id = response["user_id"]
        return bot_user_id
    except SlackApiError as e:
        print(f"Error getting bot user ID: {e.response['error']}")
        return None


# Call this function to verify the client works correctly
bot_user_id = get_bot_user_id()
