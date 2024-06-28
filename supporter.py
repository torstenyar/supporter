import re
import logging
import json
import requests
import hashlib
import os
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv

# Load the .env file
load_dotenv()

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
        log_json = json.loads(response.text)  # Assuming 'log' is a stringified JSON

        # Pretty print the JSON object
        pretty_log = json.dumps(log_json, indent=2, ensure_ascii=False)

        return pretty_log
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
        image = Image.open(BytesIO(response.content))
        image.save(f"{run_id}_screenshot.png")  # Save the image with the run_id as the filename
        return image
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
    # Parse the log file (assuming it's a JSON string)
    log_entries = json.loads(log_file)

    # Find the index of the TASK_FAILED event
    task_failed_index = next(
        (index for (index, entry) in enumerate(log_entries) if entry['eventType'] == 'TASK_FAILED'), None)

    if task_failed_index is None:
        return "No TASK_FAILED event found"

    # Traverse backwards from the TASK_FAILED event to find the last STEP_COMPLETED event
    steps_after_last_completed = []
    for i in range(task_failed_index - 1, -1, -1):
        if log_entries[i]['eventType'] == 'STEP_COMPLETED':
            # We found the last successful step before the failure
            steps_after_last_completed.append(log_entries[i])
            break
        steps_after_last_completed.append(log_entries[i])

    # Reverse the list to maintain chronological order
    steps_after_last_completed.reverse()

    # Generate the output string
    if not steps_after_last_completed:
        return "No steps found after the last STEP_COMPLETED and before TASK_FAILED"

    last_completed_step = steps_after_last_completed[0]
    failed_step = None
    skipped_steps = False
    failed_step_id = None

    # Check for skipped steps
    for i in range(1, len(steps_after_last_completed)):
        step = steps_after_last_completed[i]
        if step['eventType'] == 'STEP_FAILED':
            failed_step = step
            failed_step_id = failed_step['stepUuid']
            if i > 1:
                skipped_steps = True
            break

    main_task_step = next((step for step in steps_after_last_completed[::-1] if 'task' in step), None)
    main_task = main_task_step['task'] if main_task_step else "Unknown main task"

    # Determine the main task and any nested tasks
    nested_tasks = set(
        step['task'] for step in steps_after_last_completed if 'task' in step and step['task'] != main_task)

    if not nested_tasks:
        if skipped_steps:
            output_string = (
                f"The robot failed at step '{failed_step['name']}' following the successful completion of step '{last_completed_step['name']}', deliberately skipping some intermediate steps. "
                f"This failure occurred within the main task '{main_task.split('\\')[-1]}'."
            )
        else:
            output_string = (
                f"The robot failed at step '{failed_step['name']}' immediately after the successful completion of step '{last_completed_step['name']}'. "
                f"This failure occurred within the main task '{main_task.split('\\')[-1]}'."
            )
    else:
        nested_tasks_list = list(nested_tasks)
        if len(nested_tasks_list) == 1:
            nested_task = nested_tasks_list[0]
            if skipped_steps:
                output_string = (
                    f"The robot failed at step '{failed_step['name']}' following the successful completion of step '{last_completed_step['name']}', deliberately skipping some intermediate steps. "
                    f"This failure occurred within the task '{nested_task.split('\\')[-1]}', which is part of the main task '{main_task.split('\\')[-1]}' in this run."
                )
            else:
                output_string = (
                    f"The robot failed at step '{failed_step['name']}' immediately after the successful completion of step '{last_completed_step['name']}'. "
                    f"This failure occurred within the task '{nested_task.split('\\')[-1]}', which is part of the main task '{main_task.split('\\')[-1]}' in this run."
                )
        else:
            nested_task_hierarchy = " -> ".join(task.split('\\')[-1] for task in nested_tasks_list)
            if skipped_steps:
                output_string = (
                    f"The robot failed at step '{failed_step['name']}' following the successful completion of step '{last_completed_step['name']}', deliberately skipping some intermediate steps. "
                    f"This failure is part of the nested tasks hierarchy: {nested_task_hierarchy}. The main task in this run is '{main_task.split('\\')[-1]}'."
                )
            else:
                output_string = (
                    f"The robot failed at step '{failed_step['name']}' immediately after the successful completion of step '{last_completed_step['name']}'. "
                    f"This failure is part of the nested tasks hierarchy: {nested_task_hierarchy}. The main task in this run is '{main_task.split('\\')[-1]}'."
                )

    return output_string, failed_step_id


def generate_overview_of_changed_variables(log_file):
    # generates overview of variables that changed during the task run at which steps. -> Important for Cause Analysis
    return "overview"


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


if __name__ == '__main__':
    run_id = 'a02fb765-009e-481f-bf4e-b65156d5840d'
    log = load_log_file(run_id)

    point_of_failure_descr, failed_step_id = determine_point_of_failure(log)
    print(failed_step_id)
    print(point_of_failure_descr)
    # image = load_screenshot(run_id)
    # Decode the JSON string back into a JSON object
