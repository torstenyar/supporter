import logging
import json
import os
from azure.servicebus import ServiceBusClient, ServiceBusMessage


def send_supporter_data_to_uardi(data):
    """
    Sends the given data to the SUPPORTER_DATA_QUEUE in Azure Service Bus.
    """
    try:
        servicebus_client = ServiceBusClient.from_connection_string(conn_str=os.environ['SERVICEBUS_CONNECTION_STR'])
        with servicebus_client:
            sender = servicebus_client.get_queue_sender(queue_name=os.environ['SUPPORTER_DATA_QUEUE'])
            with sender:
                message = ServiceBusMessage(json.dumps(data))
                sender.send_messages(message)
                logging.info("Sent message to SUPPORTER_DATA_QUEUE")
    except Exception as e:
        logging.error(f"Failed to send message to queue: {e}")


def send_task_run_id_to_yarado(data):
    """
    Sends the given data to the SUPPORTER_TRIGGERED in Azure Service Bus.
    """
    try:
        servicebus_client = ServiceBusClient.from_connection_string(conn_str=os.environ['SERVICEBUS_CONNECTION_STR'])
        with servicebus_client:
            sender = servicebus_client.get_queue_sender(queue_name=os.environ['SUPPORTER_TRIGGERED'])
            with sender:
                message = ServiceBusMessage(json.dumps(data))
                sender.send_messages(message)
                logging.info("Sent task_run_id to SUPPORTER_TRIGGERED")
    except Exception as e:
        logging.error(f"Failed to send message to queue: {e}")
