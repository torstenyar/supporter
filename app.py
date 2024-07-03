from flask import Flask, request, jsonify
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from azure.monitor.opentelemetry import configure_azure_monitor
from slack_integration.event_handler import handle_event
import logging
import os


# Read the connection string from the environment variable
connection_string = os.getenv('AZURE_LOG_CONNECTION_STRING')

# Configure OpenTelemetry and Azure Monitor
resource = Resource.create(attributes={"service.name": "yarado-supporter-web-app"})
provider = TracerProvider(resource=resource)
configure_azure_monitor(
    connection_string=connection_string,
)
trace.set_tracer_provider(provider)

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)


@app.before_request
def before_request():
    ip_address = request.remote_addr

    span = trace.get_current_span()
    span.set_attribute("user.ip", ip_address)


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
