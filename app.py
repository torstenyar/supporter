from flask import Flask, request, jsonify
from slack_integration.event_handler import handle_event
import logging

app = Flask(__name__)


@app.route('/slack/events', methods=['POST'])
def slack_events():
    data = request.json
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})
    handle_event(data)
    return '', 200


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=80)
