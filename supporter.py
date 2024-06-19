import re
import logging
import json
import requests
import hashlib
import os

YARADO_API_KEY = os.getenv('YARADO_API_KEY')

PRIO_TRANSLATIONS = {
    'one': "1) Direct action required.",
    'two': "2) Action required before EoD, task needs to be completed.",
    'three': "3) Needs a look, but can wait until Yarado business hours.",
    'four': "4) No action required."
}


def extract_data_from_message(message):
    message_str = json.dumps(message)

    # Extract client name
    try:
        client_name = re.search(r'Customer: `(.*?)`', message_str).group(1)
    except AttributeError:
        logging.error("Error extracting client name")
        client_name = None

    # Extract task name
    try:
        task_name = re.search(r'Error detected in `(.*?)`', message_str).group(1)
    except AttributeError:
        logging.error("Error extracting task name")
        task_name = None

    # Extract prio
    try:
        prio = re.search(r'Prio: :(\w+):', message_str).group(1)
        prio_description = PRIO_TRANSLATIONS.get(prio, "Unknown priority")
    except AttributeError:
        logging.error("Error extracting prio")
        prio_description = None

    # Extract run ID
    try:
        run_id = re.search(r'Run ID: ([a-f0-9-]{36})\b', message_str).group(1)
    except AttributeError:
        logging.error("Error extracting run ID")
        run_id = None

    return client_name, task_name, prio_description, run_id


def get_sha256(api_key):
    return hashlib.sha256(api_key.encode()).hexdigest()

def load_log_file(run_id):
    endpoint = f"https://api.yarado.com/v1/task-runs/{run_id}/log"
    headers = {
        "X-API-KEY": get_sha256(YARADO_API_KEY)
    }
    try:
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching log file: {e}")
        return None

def load_screenshot(run_id):
    endpoint = f"https://api.yarado.com/v1/task-runs/{run_id}/screenshot"
    headers = {
        "X-API-KEY": get_sha256(YARADO_API_KEY)
    }
    try:
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching screenshot: {e}")
        return None


def load_process_description():
    # Load process descriptions and preceding step descriptions directly from azure
    return "process_description"


def load_preceding_steps():
    # Load preceding steps descriptions
    return "preceding_steps"


def determine_point_of_failure(log_file):
    # Determine the point of failure based on log file
    return "point_of_failure"


def generate_error_description(recent_steps, log_file, process_description, screenshot):
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "*Objective description:* The automated process was in the middle of processing invoice submissions. It successfully navigated to the invoice processing page and attempted to click on the 'Submit' button. However, the process failed at step 3.2 when the button became unresponsive. The screenshot shows the 'Submit' button highlighted but unclickable on the invoice processing page."
        }
    }


def perform_cause_analysis(error_description, recent_steps, log_file, process_description, screenshot):
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "*Cause analysis:* The issue appears to be caused by an application error. During the automated run, the web page took longer than expected to load, causing the 'Submit' button to remain unresponsive. This hypothesis is supported by the error logs, which show a timeout error when trying to interact with the button. Additionally, previous steps involving interactions with the same page loaded slower than usual, indicating a possible performance issue with the web application at that time."
        }
    }


def suggest_resolution(error_description, cause_analysis):
    return {
        "type": "section",
        "fields": [
            {
                "type": "mrkdwn",
                "text": "1. *Verify the web application's performance:* Check if the web application is experiencing performance issues or downtime."
            },
            {
                "type": "mrkdwn",
                "text": "2. *Adjust the wait time:* Increase the wait time in the automated process to allow more time for the web page to load fully."
            },
            {
                "type": "mrkdwn",
                "text": "3. *Retry the process:* Once the adjustments are made, retry the automated process."
            },
            {
                "type": "mrkdwn",
                "text": "4. *Monitor the process:* Ensure the process completes successfully without any timeouts."
            },
            {
                "type": "mrkdwn",
                "text": "5. *Report persistent issues:* If the problem persists, report it to the web application support team for further investigation."
            }
        ]
    }