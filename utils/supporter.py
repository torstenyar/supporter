import re
import logging
import json
import requests
import hashlib
import os
import base64

from PIL import Image
from io import BytesIO
from utils.azure_data_loader import load_task_data
from utils.azure_openai_client import initialize_client

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

        buffered = BytesIO()
        image.save(buffered, format="JPEG")
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")

        return img_str
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching screenshot: {e}")
        return None


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
                "The robot failed at step '{failed_step}' following the successful completion of step '{last_completed_step}', deliberately skipping some intermediate steps. "
                "This failure occurred within the main task '{main_task}'.".format(
                    failed_step=failed_step['name'],
                    last_completed_step=last_completed_step['name'],
                    main_task=main_task.split('\\')[-1]
                )
            )
        else:
            output_string = (
                "The robot failed at step '{failed_step}' immediately after the successful completion of step '{last_completed_step}'. "
                "This failure occurred within the main task '{main_task}'.".format(
                    failed_step=failed_step['name'],
                    last_completed_step=last_completed_step['name'],
                    main_task=main_task.split('\\')[-1]
                )
            )
    else:
        nested_tasks_list = list(nested_tasks)
        if len(nested_tasks_list) == 1:
            nested_task = nested_tasks_list[0]
            if skipped_steps:
                output_string = (
                    "The robot failed at step '{failed_step}' following the successful completion of step '{last_completed_step}', deliberately skipping some intermediate steps. "
                    "This failure occurred within the task '{nested_task}', which is part of the main task '{main_task}' in this run.".format(
                        failed_step=failed_step['name'],
                        last_completed_step=last_completed_step['name'],
                        nested_task=nested_task.split('\\')[-1],
                        main_task=main_task.split('\\')[-1]
                    )
                )
            else:
                output_string = (
                    "The robot failed at step '{failed_step}' immediately after the successful completion of step '{last_completed_step}'. "
                    "This failure occurred within the task '{nested_task}', which is part of the main task '{main_task}' in this run.".format(
                        failed_step=failed_step['name'],
                        last_completed_step=last_completed_step['name'],
                        nested_task=nested_task.split('\\')[-1],
                        main_task=main_task.split('\\')[-1]
                    )
                )
        else:
            nested_task_hierarchy = " -> ".join(task.split('\\')[-1] for task in nested_tasks_list)
            if skipped_steps:
                output_string = (
                    "The robot failed at step '{failed_step}' following the successful completion of step '{last_completed_step}', deliberately skipping some intermediate steps. "
                    "This failure is part of the nested tasks hierarchy: {nested_task_hierarchy}. The main task in this run is '{main_task}'.".format(
                        failed_step=failed_step['name'],
                        last_completed_step=last_completed_step['name'],
                        nested_task_hierarchy=nested_task_hierarchy,
                        main_task=main_task.split('\\')[-1]
                    )
                )
            else:
                output_string = (
                    "The robot failed at step '{failed_step}' immediately after the successful completion of step '{last_completed_step}'. "
                    "This failure is part of the nested tasks hierarchy: {nested_task_hierarchy}. The main task in this run is '{main_task}'.".format(
                        failed_step=failed_step['name'],
                        last_completed_step=last_completed_step['name'],
                        nested_task_hierarchy=nested_task_hierarchy,
                        main_task=main_task.split('\\')[-1]
                    )
                )

    return output_string, failed_step_id




def load_log_preceding_steps(log_file, failed_step_id, steps_to_include=10):
    # Parse the log file (assuming it's a JSON string)
    log_entries = json.loads(log_file)

    # Find the index of the failed step
    failed_step_index = next(
        (index for (index, entry) in enumerate(log_entries) if entry.get('stepUuid') == failed_step_id), None)

    if failed_step_index is None:
        return "No failed step found with the provided step ID"

    # Traverse backwards from the failed step to collect the last 10 STEP_COMPLETED steps
    preceding_steps = []
    count = 0
    for i in range(failed_step_index - 1, -1, -1):
        if log_entries[i]['eventType'] == 'STEP_COMPLETED':
            preceding_steps.append(log_entries[i])
            count += 1
            if count >= steps_to_include:
                break

    # Reverse the list to maintain chronological order
    preceding_steps.reverse()

    # Include the failed step as the last element
    preceding_steps.append(log_entries[failed_step_index])

    return preceding_steps


def load_descr_preceding_steps(preceding_steps, described_steps):
    # create list with names
    ids = [step['stepUuid'] for step in preceding_steps if 'stepUuid' in step]

    # Load preceding steps descriptions
    return "preceding_steps"


def generate_overview_of_changed_variables(log_file):
    # generates overview of variables that changed during the task run at which steps. -> Important for Cause Analysis
    return "overview"


def generate_error_description(client, customer_name, process_name, point_of_failure, steps_log, screenshot):
    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI assistant that objectively describes an occurred error in an automated workflow. "
                "The objective description should be placed in a JSON object given by the user.\n\n"
                "Context:\n"
                "You will help Yarado in delivering support to processes that run into an error. Yarado is an automation company in the Netherlands. "
                "It automates business processes using its own in-house developed software platform called the 'Yarado Client'. The automated processes are developed with the Yarado Client and hosted on Azure Virtual Machines (on which the Yarado Client is installed). "
                "The automated runs happen in the cloud/background, so no human is watching the screen when the automated process is running.\n"
                "-> The JSON code you will encounter stems from creating an automated process with our own in-house developed application called the Yarado Client. Our normal way of working is that we manage an Azure VM for our client (in this case {customer_name}), we install the Yarado Client on this VM, and then we automate their business processes using their user accounts (as if we are a new colleague).\n"
                "-> Step Flow: Yarado steps are identified by 'coords' (x.y), showing task progression from 1.1 down rows. Steps may shift across columns under conditions like unmet criteria. The task files may also include coordinates such as 'x.y.i.j'. These stem from subtask steps, where x.y still indicates the maintask step coordinates, but i.j indicate the subtask coordinate.\n\n"
                "Sometimes, these processes run into a problem/error. Because we are not actually watching what is happening on the screen, it takes a lot of time to figure out what happened. Common error types include:\n"
                "- Input errors (e.g., incorrect or invalid data)\n"
                "- Application errors (e.g., slow web browser, unresponsive application)\n"
                "- Changes in systems (e.g., missing elements, changed xpaths)\n"
                "- Typical RPA and automation errors (e.g., UI automation issues, API failures)\n\n"
                "Role:\n"
                "You will be helping us in objectively describing the occurred error, thereby helping the employees giving support to this process.\n\n"
                "Input:\n"
                f"The automated process '{process_name}' was developed for {customer_name} by Yarado.\n"
                "To be able to do the given objective, you will be provided with three main input sources (each delimited between triple '>' characters) by the user:\n"
                "1. The log data of the last 10 steps taken during the execution of this process:\n"
                f"{steps_log}\n"
                "2. The screenshot of the window that could be seen right before the error took place (see attached).\n"
            )
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Complete the mrkdwn text within the JSON object below. Return the entire JSON object only.\n"
                    "```json\n"
                    "{\n"
                    "  \"type\": \"section\",\n"
                    "  \"text\": {\n"
                    "    \"type\": \"mrkdwn\",\n"
                    f"    \"text\": \"*Objective description:* {point_of_failure}\\n\\n"
                    "[Concluding paragraph: Very short summary of the process description and how it relates to what it was doing at this moment - by investigating log file and screenshot --> Example concluding paragraph: The automated process was in the middle of processing invoice submissions. It successfully navigated to the invoice processing page and attempted to click on the 'Submit' button. However, the process failed at step 3.2 when the button became unresponsive. The screenshot shows the 'Submit' button highlighted but unclickable on the invoice processing page.]\"\n"
                    "  }\n"
                    "}\n"
                    "```"
                )},
                {"type": "image_url",
                 "image_url": {
                     "url": f"data:image/jpeg;base64,{screenshot}"
                     }
                 }
            ]
        }
    ]

    response = client.chat.completions.create(
        model="generate_descriptions",
        messages=messages,
        temperature=0.3,
        response_format={"type": "json_object"}
    )

    return json.loads(response.choices[0].message.content)


def perform_cause_analysis():
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "*Cause analysis:* The issue appears to be caused by an application error. During the automated run, the web page took longer than expected to load, causing the 'Submit' button to remain unresponsive. This hypothesis is supported by the error logs, which show a timeout error when trying to interact with the button. Additionally, previous steps involving interactions with the same page loaded slower than usual, indicating a possible performance issue with the web application at that time."
        }
    }


def suggest_resolution():
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
    run_id = 'f17122e2-99b9-41bf-9159-6d39a422c788'
    client_name = 'Nieuwe Stroom'
    task_name = 'Nieuwe-Stroom-MinderNL-Main'

    client = initialize_client()

    log = load_log_file(run_id)
    image = load_screenshot(run_id)

    point_of_failure_descr, failed_step_id = determine_point_of_failure(log)

    # Load the preceding steps
    preceding_steps_log = load_log_preceding_steps(log, failed_step_id)

    process_row, task_data, az_record_found = load_task_data(customer_name=client_name, process_name=task_name)

    process_description = None
    preceding_steps_descr = None

    if az_record_found:
        process_description = process_row['ProcessDescription']
        preceding_steps_descr = load_descr_preceding_steps(preceding_steps_log, task_data)

    error_description = generate_error_description(client, client_name, task_name, point_of_failure_descr, preceding_steps_log, image)

    # Decode the JSON string back into a JSON object
