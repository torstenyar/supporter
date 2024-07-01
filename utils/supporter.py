import re
import logging
import json
import requests
import hashlib
import os
import base64

# Uncomment below for local testing
#from dotenv import load_dotenv

# Load environment variables from .env file
#load_dotenv()

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
    failed_step = None
    skipped_steps = False
    failed_step_id = None

    # Parse the log file (assuming it's a JSON string)
    log_entries = json.loads(log_file)

    # Find the index of the TASK_FAILED event
    task_failed_index = next(
        (index for (index, entry) in enumerate(log_entries) if entry['eventType'] == 'TASK_FAILED'), None)

    if task_failed_index is None:
        return "No TASK_FAILED event found", None

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
    if len(steps_after_last_completed) <= 1:
        return "No steps were found after the last STEP_COMPLETED and before TASK_FAILED, meaning the task failed without any specific step failing.", \
            steps_after_last_completed[0]['stepUuid']

    last_completed_step = steps_after_last_completed[0]

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
    main_task_loop = main_task_step['loop'] if main_task_step else "Unknown loop"

    # Determine the main task and any nested tasks
    nested_tasks = set(
        (step['task'], step['loop']) for step in steps_after_last_completed if
        'task' in step and step['task'] != main_task)

    if not nested_tasks:
        if skipped_steps:
            output_string = (
                "The robot failed at step '{failed_step}' (which was part of loop row number {failed_loop}) following the successful completion of step '{last_completed_step}' (which was part of loop row number {last_completed_loop}), deliberately skipping some intermediate steps. "
                "This failure occurred within the main task '{main_task}' (which was part of loop row number {main_task_loop}).".format(
                    failed_step=failed_step['name'],
                    failed_loop=failed_step['loop'],
                    last_completed_step=last_completed_step['name'],
                    last_completed_loop=last_completed_step['loop'],
                    main_task=main_task.split('\\')[-1],
                    main_task_loop=main_task_loop
                )
            )
        else:
            output_string = (
                "The robot failed at step '{failed_step}' (which was part of loop row number {failed_loop}) immediately after the successful completion of step '{last_completed_step}' (which was part of loop row number {last_completed_loop}). "
                "This failure occurred within the main task '{main_task}' (which was part of loop row number {main_task_loop}).".format(
                    failed_step=failed_step['name'],
                    failed_loop=failed_step['loop'],
                    last_completed_step=last_completed_step['name'],
                    last_completed_loop=last_completed_step['loop'],
                    main_task=main_task.split('\\')[-1],
                    main_task_loop=main_task_loop
                )
            )
    else:
        nested_tasks_list = list(nested_tasks)
        if len(nested_tasks_list) == 1:
            nested_task, nested_loop = nested_tasks_list[0]
            if skipped_steps:
                output_string = (
                    "The robot failed at step '{failed_step}' (which was part of loop row number {failed_loop}) following the successful completion of step '{last_completed_step}' (which was part of loop row number {last_completed_loop}), deliberately skipping some intermediate steps. "
                    "This failure occurred within the task '{nested_task}' (loop {nested_loop}), which is part of the main task '{main_task}' (which was part of loop row number {main_task_loop}) in this run.".format(
                        failed_step=failed_step['name'],
                        failed_loop=failed_step['loop'],
                        last_completed_step=last_completed_step['name'],
                        last_completed_loop=last_completed_step['loop'],
                        nested_task=nested_task.split('\\')[-1],
                        nested_loop=nested_loop,
                        main_task=main_task.split('\\')[-1],
                        main_task_loop=main_task_loop
                    )
                )
            else:
                output_string = (
                    "The robot failed at step '{failed_step}' (which was part of loop row number {failed_loop}) immediately after the successful completion of step '{last_completed_step}' (which was part of loop row number {last_completed_loop}). "
                    "This failure occurred within the task '{nested_task}' (loop {nested_loop}), which is part of the main task '{main_task}' (which was part of loop row number {main_task_loop}) in this run.".format(
                        failed_step=failed_step['name'],
                        failed_loop=failed_step['loop'],
                        last_completed_step=last_completed_step['name'],
                        last_completed_loop=last_completed_step['loop'],
                        nested_task=nested_task.split('\\')[-1],
                        nested_loop=nested_loop,
                        main_task=main_task.split('\\')[-1],
                        main_task_loop=main_task_loop
                    )
                )
        else:
            nested_task_hierarchy = " -> ".join(
                f"{task.split('\\')[-1]} (loop {loop})" for task, loop in nested_tasks_list)
            if skipped_steps:
                output_string = (
                    "The robot failed at step '{failed_step}' (which was part of loop row number {failed_loop}) following the successful completion of step '{last_completed_step}' (which was part of loop row number {last_completed_loop}), deliberately skipping some intermediate steps. "
                    "This failure is part of the nested tasks hierarchy: {nested_task_hierarchy}. The main task in this run is '{main_task}' (which was part of loop row number {main_task_loop}).".format(
                        failed_step=failed_step['name'],
                        failed_loop=failed_step['loop'],
                        last_completed_step=last_completed_step['name'],
                        last_completed_loop=last_completed_step['loop'],
                        nested_task_hierarchy=nested_task_hierarchy,
                        main_task=main_task.split('\\')[-1],
                        main_task_loop=main_task_loop
                    )
                )
            else:
                output_string = (
                    "The robot failed at step '{failed_step}' (which was part of loop row number {failed_loop}) immediately after the successful completion of step '{last_completed_step}' (which was part of loop row number {last_completed_loop}). "
                    "This failure is part of the nested tasks hierarchy: {nested_task_hierarchy}. The main task in this run is '{main_task}' (which was part of loop row number {main_task_loop}).".format(
                        failed_step=failed_step['name'],
                        failed_loop=failed_step['loop'],
                        last_completed_step=last_completed_step['name'],
                        last_completed_loop=last_completed_step['loop'],
                        nested_task_hierarchy=nested_task_hierarchy,
                        main_task=main_task.split('\\')[-1],
                        main_task_loop=main_task_loop
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
        if log_entries[i]['eventType'] == 'STEP_COMPLETED' or log_entries[i]['eventType'] == 'STEP_FAILED':
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


def extract_variable_changes(log_data):
    log_entries = json.loads(log_data)
    variable_changes = {}
    for log in log_entries:
        if 'changedVariables' in log and log['changedVariables']:
            for variable in log['changedVariables']:
                if variable['name'] not in variable_changes:
                    variable_changes[variable['name']] = []
                variable_changes[variable['name']].append({
                    'timestamp': log['timestamp'],
                    'oldValue': variable['oldValue'],
                    'newValue': variable['newValue'],
                    'stepId': log['stepId'],
                    'loop': log['loop']
                })
    return variable_changes


def extract_relevant_variables(preceding_log_steps):
    relevant_variables = set()
    for step in preceding_log_steps:
        for key, value in step.items():
            if isinstance(value, str) and '%' in value:
                variables = [v for v in value.split() if v.startswith('%') and v.endswith('%')]
                relevant_variables.update(variables)
    return relevant_variables


def generate_textual_overview(log_data, preceding_log_steps):
    variable_changes = extract_variable_changes(log_data)
    relevant_variables = extract_relevant_variables(preceding_log_steps)

    overview_lines = ["### Variable Changes Overview", "", "#### Introduction:",
                      "This overview provides a detailed account of variable changes that occurred during the execution of the automated process. Each change is documented with the associated step ID and loop number.",
                      "", "#### Variable Changes:"]

    if not relevant_variables:
        overview_lines.append(
            "No variables changed within the included window of {steps} preceding log steps.".format(steps=len(preceding_log_steps) - 1))
    else:
        filtered_variable_changes = {var: changes for var, changes in variable_changes.items() if var in relevant_variables}

        if not filtered_variable_changes:
            overview_lines.append(
                "No relevant variable changes found within the included window of {steps} preceding log steps.".format(steps=len(preceding_log_steps) - 1))
        else:
            for variable, changes in filtered_variable_changes.items():
                initial_value = changes[0]['oldValue']
                overview_lines.append("\n1. **Variable: {variable}**".format(variable=variable))
                overview_lines.append("   - Initial Value: \"{initial_value}\"".format(initial_value=initial_value))
                overview_lines.append("   - Changes:")
                for change in changes:
                    overview_lines.append(
                        "     - Step ID: {stepId} | Loop: {loop} | New Value: \"{newValue}\"".format(
                            stepId=change['stepId'], loop=change['loop'], newValue=change['newValue']))

    overview_lines.append("\n### End of Overview")
    return "\n".join(overview_lines)


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
                "Audience:\n"
                "Your target audience is the employees of Yarado who provide support to processes running into problems. Errors pop up in Slack, triggering the Yarado-supporter (the name of the AI model) to provide support. The output will also be sent in Slack.\n\n"
                "Input:\n"
                "The automated process '{process_name}' was developed for {customer_name} by Yarado.\n"
                "To be able to do the given objective, you will be provided with two main input sources (each delimited between triple '>' characters):\n"
                "1. The log data of the last {steps} steps taken during the execution of this process:\n>>>\n"
                "{steps_log}\n>>>\n"
                "2. The screenshot of the window that could be seen right before the error took place (see attached).\n"
            ).format(customer_name=customer_name, process_name=process_name, steps=len(steps_log) - 1, steps_log=steps_log)
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Complete the mrkdwn text within the JSON object below. Return the entire JSON object only.\n"
                    "```json\n"
                    "{{\n"
                    "  \"type\": \"section\",\n"
                    "  \"text\": {{\n"
                    "    \"type\": \"mrkdwn\",\n"
                    "    \"text\": \"*Objective description:* {point_of_failure}\\n\\n"
                    "[Concluding paragraph: Very short summary of the process description and how it relates to what it was doing at this moment - by investigating log file and screenshot --> Example concluding paragraph: The automated process was in the middle of processing invoice submissions. It successfully navigated to the invoice processing page and attempted to click on the 'Submit' button. However, the process failed at step 3.2 when the button became unresponsive. The screenshot shows the 'Submit' button highlighted but unclickable on the invoice processing page.]\"\n"
                    "  }}\n"
                    "}}\n"
                    "```"
                ).format(point_of_failure=point_of_failure)},
                {"type": "image_url",
                 "image_url": {
                     "url": "data:image/jpeg;base64,{screenshot}".format(screenshot=screenshot)
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


def perform_cause_analysis(client, customer_name, process_name, preceding_steps_log, screenshot, error_description, variable_changes):
    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI assistant tasked with performing a cause analysis for an error in an automated workflow. "
                "Your goal is to analyze the provided information and identify the underlying cause of the error, providing a detailed explanation. "
                "You should look beyond the immediate step where the error occurred and consider earlier events that could have contributed to the failure. "
                "Iteratively reason backwards through the steps and variable changes to find the root cause, even if it happened much earlier in the process.\n\n"
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
                "Audience:\n"
                "Your target audience is the employees of Yarado who provide support to processes running into problems. Errors pop up in Slack, triggering the Yarado-supporter (the name of the AI model) to provide support. The output will also be sent in Slack.\n\n"
                "Input Sources Description:\n"
                "The automated process '{process_name}' was developed for {customer_name} by Yarado.\n"
                "1. The log data of the last {steps} steps taken during the execution of this process: This provides a detailed account of the steps leading up to the error, helping to identify any anomalies or irregularities.\n"
                "2. A screenshot of the window that could be seen right before the error took place: This offers visual context, showing the state of the application at the point of failure.\n"
                "3. The objective error description generated by the AI: This gives a concise summary of the error, providing a clear starting point for the analysis. Note: This is given in JSON format. Note that this is information already given to the end user, so do not repeat this in your generated output.\n"
                "4. An overview of variable changes that occurred during the execution of the process: This helps identify any changes in the state of variables that could have contributed to the error.\n\n"
                "Task Complexity:\n"
                "Your task is to pinpoint why the error occurred and explain why you think so. It is important that you conduct a deep analysis, considering events that might have contributed to the error even if they happened earlier in the task run. The model should take its time to thoroughly analyze the data and look further than just the obvious."
                "\nInclude arguments and proof on why you think it is the reason why the automated process failed. Refer to the different sources of input. However, remember that someone giving support does not know that we created the variables overview, and that we only looked at the preceding {steps} steps. Therefore mention sources they do know (i.e. logfile, from which the variable overview and preceding steps are taken from, or the screenshot, in combination with the previously generated error description).\n\n"
                "Example:\n"
                "In a previous analysis, the failure occurred during step 8.4 'Trigger error' because the process was unable to find the input file it was looking for. This was evident from earlier steps logged in the process. Specifically, in step 5.1, the directory 'C:\\Users\\ENE01\\OneDrive - 1Energielabel\\Registraties\\Juni 2024' was not found, which led to the variable '%input_file_exists%' being set to 'False' in step 6.1. Consequently, the condition in step 7.1 'Input file exists?' was unsatisfied, leading to the process displaying a message that the input file does not exist in step 7.4. Finally, in step 8.4 'Trigger error', the process attempted to proceed but failed due to the missing input file. The root cause, however, was identified as 'Sharepoint not syncing so the robot cannot find the file'.\n\n"
                "Additional Examples:\n"
                "These examples show a potential obvious cause versus an underlying deeper root cause. Always ensure you iteratively reason backwards to find the potential root cause."
                "1. An error occurred at step 10.2 'Submit form' because the form could not be submitted. Looking back, step 7.3 'Fill form' showed a slow response time from the server, causing delays. Further investigation revealed that in step 5.1 'Load page', the page took unusually long to load, indicating a network issue. The root cause was identified as 'Network congestion causing delays in loading and form submission'.\n"
                "2. A failure happened at step 12.5 'Validate data' due to incorrect data format. Tracing back, step 9.1 'Fetch data from API' logged the received data as incomplete. Checking earlier, step 6.4 'API request' indicated a timeout warning, suggesting intermittent connectivity issues. The root cause was identified as 'Unstable internet connection leading to incomplete data retrieval from API'.\n"
                "3. An error occurred at step 9.3 'Save record' because the database was unresponsive. Looking back, step 6.2 'Connect to database' showed a connection delay. Further analysis revealed that in step 4.1 'Initialize database connection', a warning about high database load was logged. The root cause was identified as 'High database load causing delayed responses and unavailability'.\n"
                "4. A failure happened at step 11.4 'Generate report' due to missing data fields. Tracing back, step 8.2 'Aggregate data' showed incomplete data sets. Investigating further, step 5.5 'Fetch data from source' indicated partial data retrieval. The root cause was identified as 'Data source server outage leading to incomplete data retrieval'.\n"
                "5. An issue occurred at step 7.1 'Send email' because the email server rejected the request. Looking back, step 6.3 'Prepare email content' logged an unusually large attachment. Further analysis of step 4.4 'Fetch attachment' revealed that the file size exceeded the email server's limit. The root cause was identified as 'Attachment size exceeding email server limit causing email rejection'.\n\n"
                "Output Format:\n"
                "You should provide the cause analysis in the form of a JSON object. Return the entire JSON object only.\n"
                "```json\n"
                "{{\n"
                "  \"type\": \"section\",\n"
                "  \"text\": {{\n"
                "    \"type\": \"mrkdwn\",\n"
                "    \"text\": \"*Cause analysis:* [Detailed cause analysis text]\"\n"
                "  }}\n"
                "}}\n"
                "```\n"
                "Input sources data:\n"
                "Objective error description in JSON format:\n"
                "{error_description}\n\n"
                "Point of failure description:\n"
                "{point_of_failure_descr}\n\n"
                "Overview of variable changes:\n"
                "{variable_changes}\n\n"
                "Log data of the last {steps} steps in JSON format:\n"
                "{preceding_steps_log}\n\n"
                "Screenshot -> see attached image."
            ).format(
                customer_name=customer_name,
                process_name=process_name,
                steps=len(preceding_steps_log) - 1,
                error_description=json.dumps(error_description),
                point_of_failure_descr=point_of_failure_descr,
                variable_changes=variable_changes,
                preceding_steps_log=json.dumps(preceding_steps_log)
            )
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Complete the mrkdwn text within the JSON object below by filling out the placeholder. Remember that it should form the answer to the question: 'Why did it go wrong? What led me to believe this is the case?'. Return the entire filled out JSON object only.\n"
                    "```json\n"
                    "{{\n"
                    "  \"type\": \"section\",\n"
                    "  \"text\": {{\n"
                    "    \"type\": \"mrkdwn\",\n"
                    "    \"text\": \"*Cause analysis:* [Detailed cause analysis based on all the provided information and your in-depth reasoning - Constrain yourself to iteratively reasoning backwards to find the root cause that led to this error.]\"\n"
                    "  }}\n"
                    "}}\n"
                    "```"
                )},
                {"type": "image_url",
                 "image_url": {
                     "url": "data:image/jpeg;base64,{screenshot}".format(screenshot=screenshot)
                 }
                }
            ]
        }
    ]

    response = client.chat.completions.create(
        model="generate_descriptions",
        messages=messages,
        temperature=0.9,
        response_format={"type": "json_object"}
    )

    return json.loads(response.choices[0].message.content)


if __name__ == '__main__':
    run_id = '3bf9bdf7-9a96-4113-9d2e-bc31285ca7f1'
    client_name = '1energielabel'
    task_name = 'record_processing_monday'

    client = initialize_client()

    log = load_log_file(run_id)
    image = load_screenshot(run_id)

    point_of_failure_descr, failed_step_id = determine_point_of_failure(log)

    # Load the preceding steps
    preceding_steps_log = load_log_preceding_steps(log, failed_step_id, steps_to_include=10)

    variable_changes = generate_textual_overview(log, preceding_steps_log)

    process_row, task_data, az_record_found = load_task_data(customer_name=client_name, process_name=task_name)

    process_description = None
    preceding_steps_descr = None

    if az_record_found:
        process_description = process_row['ProcessDescription']
        preceding_steps_descr = load_descr_preceding_steps(preceding_steps_log, task_data)

    error_description = generate_error_description(client, client_name, task_name, point_of_failure_descr,
                                                   preceding_steps_log, image)

    cause_analysis = perform_cause_analysis(client, client_name, task_name, preceding_steps_log, image,
                                            error_description, variable_changes)

    print(f'Error description:\n\n{error_description}\n\n')

    print(f'Cause analysis:\n\n{cause_analysis}')
