from flask import Flask, request, jsonify
from slack_integration.event_handler import handle_event
import logging
# Import the `configure_azure_monitor()` function from the `azure.monitor.opentelemetry` package.
from azure.monitor.opentelemetry import configure_azure_monitor
import os
# Configure OpenTelemetry to use Azure Monitor with the specified connection string.

# Read the connection string from the environment variable
connection_string = os.getenv('AZURE_LOG_CONNECTION_STRING')

configure_azure_monitor(
    connection_string=connection_string,
)

app = Flask(__name__)


@app.route('/slack/events', methods=['POST'])
def slack_events():
    logging.info('Starting Yarado supporter...')
    data = request.json
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})
    handle_event(data)
    return '', 200


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=80)
