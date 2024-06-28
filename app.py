from flask import Flask, request, jsonify
from slack_integration.event_handler import handle_event
from dotenv import load_dotenv
import logging

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.DEBUG)

@app.route('/')
def hello():
    app.logger.debug('Handling request to root endpoint')
    return "Hello World!"


@app.route('/slack/events', methods=['POST'])
def slack_events():
    data = request.json
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})
    handle_event(data)
    return '', 200


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=80)
