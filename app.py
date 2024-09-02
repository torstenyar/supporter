import os
import asyncio
import logging
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from slack_integration.event_handler import handle_event
from slack_integration.slack_client import initialize_slack_client
from utils.azure_openai_client import initialize_openai_client
from azure.monitor.opentelemetry import configure_azure_monitor

# Load environment variables
load_dotenv()

# Set up logging
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(level=getattr(logging, LOG_LEVEL), format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Custom filter to exclude specific log messages from being sent to Azure Monitor
class AzureMonitorFilter(logging.Filter):
    def filter(self, record):
        # Exclude specific log messages
        excluded_messages = [
            "Request URL:",
            "Response status:",
            "Transmission succeeded:"
        ]
        return not any(msg in record.getMessage() for msg in excluded_messages)


# Apply the custom filter to the root logger
logger.addFilter(AzureMonitorFilter())

# Reduce verbosity for Azure SDK logging
logging.getLogger('azure').setLevel(logging.WARNING)
logging.getLogger('azure.core').setLevel(logging.WARNING)

# Configure OpenTelemetry with Azure Monitor
connection_string = os.getenv('AZURE_LOG_CONNECTION_STRING')

if connection_string:
    try:
        configure_azure_monitor(connection_string=connection_string)
        logger.info("Azure Monitor configured successfully.")
    except Exception as e:
        logger.error(f"Failed to configure Azure Monitor: {e}")
else:
    logger.warning("Azure Monitor connection string not provided. Skipping Azure Monitor configuration.")

# Set up tracing
resource = Resource.create(attributes={"service.name": "yarado-supporter-web-app"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)

# Initialize environment-specific configurations
ENVIRONMENT = os.getenv('YARADO_ENVIRONMENT', 'production')

if ENVIRONMENT == 'development':
    slack_bot_token = os.getenv('SLACK_BOT_TOKEN_DEV')
    port = 8080
else:
    slack_bot_token = os.getenv('SLACK_BOT_TOKEN_PROD')
    port = int(os.environ.get('PORT', 80))

# Initialize Slack client
slack_client = initialize_slack_client(slack_bot_token)
azure_openai_client = initialize_openai_client()

azure_api_key = os.getenv('AZURE_API_KEY')
servicebus_connection_str = os.getenv('SERVICEBUS_CONNECTION_STR')
supporter_data_queue = os.getenv('SUPPORTER_DATA_QUEUE')


# Custom filter to exclude health check logs
class HealthCheckFilter(logging.Filter):
    def filter(self, record):
        return 'GET /health' not in record.getMessage()


# Apply the HealthCheckFilter to all log handlers
for handler in logger.handlers:
    handler.addFilter(HealthCheckFilter())

# Initialize the Flask app
app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)


# Asynchronous function to handle the Slack event
async def async_handle_event(data, environment, slack_client, azure_openai_client):
    await handle_event(data, environment, slack_client, azure_openai_client)


@app.route('/slack/events', methods=['POST'])
def slack_events():
    logger.info('Receiving Slack event...')
    data = request.json
    if not data or "type" not in data:
        logger.warning('Received non-Slack event request. Ignoring.')
        return '', 200
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    # Use asyncio.run to execute the async function in the synchronous Flask context
    asyncio.run(async_handle_event(data, ENVIRONMENT, slack_client, azure_openai_client))

    return '', 200


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    logger.info(f"Starting Yarado Supporter in {ENVIRONMENT} environment on port {port}")
    app.run(host='0.0.0.0', port=port)
