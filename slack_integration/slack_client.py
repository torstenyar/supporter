from slack_sdk import WebClient
import os

slack_token = os.getenv("SLACK_BOT_TOKEN")
client = WebClient(token=slack_token)

bot_user_id = None


def get_bot_user_id():
    global bot_user_id
    response = client.auth_test()
    bot_user_id = response['user_id']


get_bot_user_id()
