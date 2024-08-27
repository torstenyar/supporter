import os
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from slack_integration.event_handler import handle_event
from slack_integration.slack_client import initialize_slack_client
import logging

load_dotenv()

ENVIRONMENT = os.getenv('YARADO_ENVIRONMENT', 'production')

# Use environment-specific Slack token
if ENVIRONMENT == 'development':
    slack_bot_token = os.getenv('SLACK_BOT_TOKEN_DEV')
    port = 8080
else:
    slack_bot_token = os.getenv('SLACK_BOT_TOKEN_PROD')
    port = int(os.environ.get('PORT', 80))

# Initialize Slack client
slack_client = initialize_slack_client(slack_bot_token)

# Other environmental variables remain the same for both environments
connection_string = os.getenv('AZURE_LOG_CONNECTION_STRING')
azure_api_key = os.getenv('AZURE_API_KEY')
servicebus_connection_str = os.getenv('SERVICEBUS_CONNECTION_STR')
supporter_data_queue = os.getenv('SUPPORTER_DATA_QUEUE')


class HealthCheckFilter(logging.Filter):
    def filter(self, record):
        return 'GET /health' not in record.getMessage()


# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Add the HealthCheckFilter to the logger
for handler in logger.handlers:
    handler.addFilter(HealthCheckFilter())

resource = Resource.create(attributes={"service.name": "yarado-supporter-web-app"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)

if connection_string:
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=connection_string)
        logger.info("Azure Monitor configured successfully.")
    except Exception as e:
        logger.error(f"Failed to configure Azure Monitor: {e}")
else:
    logger.warning("Azure Monitor connection string not provided. Skipping Azure Monitor configuration.")

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)


@app.before_request
def before_request():
    ip_address = request.remote_addr
    span = trace.get_current_span()
    span.set_attribute("user.ip", ip_address)


@app.route('/slack/events', methods=['POST'])
def slack_events():
    logging.info('Receiving Slack event...')
    data = request.json
    if not data or "type" not in data:
        logging.warning('Received non-Slack event request. Ignoring.')
        return '', 200
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})
    handle_event(data, ENVIRONMENT, slack_client)
    return '', 200


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    print(f"Starting Yarado Supporter in {ENVIRONMENT} environment on port {port}")
    app.run(host='0.0.0.0', port=port)
