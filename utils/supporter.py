import re
import logging
import json
import requests
import hashlib
import os
import base64
import time
from collections import defaultdict
import random

# Uncomment below for local testing
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

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
    endpoint = "https://api.yarado.com/v1/task-runs/{}/log".format(run_id)
    headers = {
        "X-API-KEY": get_sha256(YARADO_API_KEY)
    }
    CHARACTER_LIMIT = 30000  # Define the character limit for log values

    try:
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()

        try:
            log_json = json.loads(response.text)

            # Iterate over key-value pairs and replace values exceeding the character limit
            def truncate_large_values(d, limit):
                if isinstance(d, dict):
                    return {k: (truncate_large_values(v, limit) if isinstance(v, (dict, list)) else (v if len(
                        str(v)) <= limit else "hidden long string [{}]...".format(len(str(v))))) for k, v in d.items()}
                elif isinstance(d, list):
                    return [truncate_large_values(i, limit) for i in d]
                return d

            log_json = truncate_large_values(log_json, CHARACTER_LIMIT)

            # Pretty print the JSON object
            pretty_log = json.dumps(log_json, indent=2, ensure_ascii=False)

            return pretty_log
        except json.JSONDecodeError:
            logging.error("Log file is not in JSON format.")
            return "INVALID_JSON"

    except requests.exceptions.RequestException as e:
        logging.error("Error fetching log file: {}".format(e))
        return None


def load_screenshot(run_id):
    endpoint = "https://api.yarado.com/v1/task-runs/{}/screenshot".format(run_id)
    headers = {
        "X-API-KEY": get_sha256(YARADO_API_KEY)
    }
    try:
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()

        try:
            image = Image.open(BytesIO(response.content))
            buffered = BytesIO()
            # Save as PNG instead of JPEG
            image.save(buffered, format="PNG")
            img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
            return img_str
        except IOError:
            logging.error("Error processing the screenshot image.")
            return "INVALID_IMAGE"

    except requests.exceptions.RequestException as e:
        logging.error("Error fetching screenshot: {}".format(e))
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
                "{} (loop {})".format(task.split('\\')[-1], loop) for task, loop in nested_tasks_list)
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
    variable_changes = defaultdict(list)
    for log in log_entries:
        if 'changedVariables' in log and log['changedVariables']:
            task = log.get('task', 'N/A')
            if task != 'N/A':
                task = task.split('\\')[-1]
            for variable in log['changedVariables']:
                variable_changes[variable['name']].append({
                    'timestamp': log.get('timestamp', 'N/A'),  # Use .get method with a default value
                    'oldValue': variable.get('oldValue', 'N/A'),  # Use .get method with a default value
                    'newValue': variable.get('newValue', 'N/A'),  # Use .get method with a default value
                    'stepId': log.get('stepId', 'N/A'),  # Use .get method with a default value
                    'loop': log.get('loop', 'N/A'),  # Use .get method with a default value
                    'task': task
                })
    return dict(variable_changes)


def extract_relevant_variables(preceding_log_steps):
    relevant_variables = set()
    variable_pattern = re.compile(r'%[^%]+%')

    for step in preceding_log_steps:
        for key, value in step.items():
            if isinstance(value, str) and '%' in value:
                variables = variable_pattern.findall(value)
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
            "No variables changed within the included window of {steps} preceding log steps.".format(
                steps=len(preceding_log_steps) - 1))
    else:
        filtered_variable_changes = {var: changes for var, changes in variable_changes.items() if
                                     var in relevant_variables}
        if not filtered_variable_changes:
            overview_lines.append(
                "No relevant variable changes found within the included window of {steps} preceding log steps.".format(
                    steps=len(preceding_log_steps) - 1))
        else:
            for variable, changes in filtered_variable_changes.items():
                overview_lines.append("* {}:".format(variable))
                for change in changes:
                    overview_lines.append(
                        "  Old Value: {oldValue}, New Value: {newValue}, Step ID: {stepId}, Loop: {loop}, Task: {task}".format(
                            oldValue=change['oldValue'],
                            newValue=change['newValue'],
                            stepId=change['stepId'],
                            loop=change['loop'],
                            task=change['task']
                        ))
    overview_lines.append("\n### End of Overview")
    return "\n".join(overview_lines)


def retry_request(client, messages, model="generate_descriptions", max_retries=5, initial_timeout=1, max_timeout=60,
                  max_tokens=4096):
    for attempt in range(max_retries):
        try:
            print(f"Attempt {attempt + 1} of {max_retries}...")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={ "type": "json_object" },
                temperature=0.2,
                top_p=0.7,
                max_tokens=max_tokens,
                frequency_penalty=0.5,
                presence_penalty=0.0,
                timeout=90,
                seed=42
            )
            print(f"Request successful on attempt {attempt + 1}")
            ai_generated_json = response.choices[0].message.content
            return extract_and_create_json_response(ai_generated_json)
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"Max retries reached. Last error: {e}")
                logging.error(f"Max retries reached. Last error: {e}")
                error_message = f":warning: Error: Azure OpenAI did not respond successfully after multiple attempts. \n\nLast error: \n```{str(e)}```\n\nPlease try again later."
                return extract_and_create_json_response(json.dumps({"text": error_message}))

            wait_time = min(initial_timeout * (2 ** attempt) + random.uniform(0, 1), max_timeout)
            print(f"Attempt {attempt + 1} failed. Retrying in {wait_time:.2f} seconds. Error: {e}")
            logging.warning(f"Attempt {attempt + 1} failed. Retrying in {wait_time:.2f} seconds. Error: {e}")
            time.sleep(wait_time)

    # This line should never be reached due to the return statement in the loop, but it's here for completeness
    return extract_and_create_json_response(
        json.dumps({"text": ":warning: Unexpected error occurred during API request."}))


def convert_to_slack_format(text):
    # Convert bold (** or __) to Slack bold (*)
    text = re.sub(r'(\*\*|__)(.*?)\1', r'*\2*', text)

    # Convert italic (* or _) to Slack italic (_)
    text = re.sub(r'(\*|_)(.*?)\1', r'_\2_', text)

    # Convert strikethrough (~~) to Slack strikethrough (~)
    text = re.sub(r'~~(.*?)~~', r'~\1~', text)

    # Convert inline code (`) to Slack inline code (`)
    # No change needed as it's the same in both

    # Convert code blocks (```) to Slack code blocks (```)
    # No change needed as it's the same in both

    # Convert blockquotes (> ) to Slack blockquotes (>)
    text = re.sub(r'^>\s', '>', text, flags=re.MULTILINE)

    # Convert markdown links to Slack links
    text = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<\2|\1>', text)

    # Convert markdown headers to Slack bold text
    text = re.sub(r'^#{1,6}\s*(.+)$', r'*\1*', text, flags=re.MULTILINE)

    return text


def extract_and_create_json_response(ai_generated_content):
    try:
        # Try to parse the AI-generated content as JSON
        try:
            parsed_json = json.loads(ai_generated_content)
        except json.JSONDecodeError:
            # If it's not valid JSON, treat the entire content as text
            parsed_json = {"text": ai_generated_content}

        # Function to recursively search for the 'text' key
        def find_text_content(obj):
            if isinstance(obj, dict):
                if 'text' in obj and isinstance(obj['text'], str):
                    return obj['text']
                for value in obj.values():
                    result = find_text_content(value)
                    if result:
                        return result
            elif isinstance(obj, list):
                for item in obj:
                    result = find_text_content(item)
                    if result:
                        return result
            return None

        # Find the text content
        content = find_text_content(parsed_json)

        if content is None:
            raise ValueError("No 'text' key with string content found in the content")

        # Convert the content to Slack format
        slack_formatted_content = convert_to_slack_format(content)

        # Create the final JSON response
        final_response = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": slack_formatted_content
            }
        }

        return json.dumps(final_response, ensure_ascii=False)

    except ValueError as ve:
        # Handle the case where no valid text content is found
        error_message = f"Error: {str(ve)}"
        return json.dumps({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": error_message
            }
        })
    except Exception as e:
        # Handle any other exceptions
        error_message = f"Error processing the AI response: {str(e)}"
        return json.dumps({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": error_message
            }
        })


def generate_error_description(client, customer_name, process_name, point_of_failure, steps_log, screenshot):
    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI assistant designed to help Yarado support staff by objectively describing errors in automated workflows. "
                "Your descriptions need to be formatted in JSON format and are used directly in Slack messages to inform Yarado employees about process failures.\n\n"
                "Context:\n"
                "Yarado is an automation company that uses its proprietary 'Yarado Client' software to automate business processes. "
                "These processes run on Azure Virtual Machines without human supervision. "
                "The process '{process_name}' was developed for the customer '{customer_name}'.\n\n"
                "Your task:\n"
                "Provide a clear, concise, and objective description of the error that occurred. Focus ONLY on observable facts and DO NOT speculate about causes or offer any analysis. "
                "Your role is strictly to describe what happened, not why it happened. The cause analysis will be performed separately.\n\n"
                "Important:\n"
                "- DO NOT attempt to explain why the error occurred or what might have caused it.\n"
                "- Stick to describing the observable symptoms and effects of the error.\n"
                "- If you're tempted to use phrases like 'because', 'due to', or 'caused by', stop and rephrase without speculation.\n"
                "Input sources:\n"
                "1. Point of failure description:\n>>>\n{point_of_failure}\n>>>\n"
                "2. Log data of the last {steps} steps:\n>>>\n{steps_log}\n>>>\n"
                "3. Screenshot of the window just before the error (attached to this message).\n\n"
                "Formatting Guidelines for Slack messages:\n"
                "- Use *asterisks* for bold text\n"
                "- Use _underscores_ for italic text\n"
                "- Use ~tildes~ for strikethrough text\n"
                "- Use `backticks` for inline code\n"
                "- Use ```triple backticks``` for code blocks\n"
                "- Use > for blockquotes\n"
                "- Use :emoji_name: for emojis\n"
                "- Use \\n for line breaks\n\n"
                "Remember, these are Slack-specific formatting rules, not standard markdown."
            ).format(
                customer_name=customer_name,
                process_name=process_name,
                steps=len(steps_log) - 1,
                point_of_failure=point_of_failure,
                steps_log=json.dumps(steps_log, indent=2)
            )
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Provide a structured description of the error, including:\n"
                    "1. *Error Location*: Specify the exact step, loop, and task where the error occurred. Include the step coordinates (e.g., 5.2) and indicate if it's in the first loop.\n"
                    "2. *Observed Behavior*: Describe what happened, focusing ONLY on observable facts from the log and screenshot. DO NOT speculate on causes.\n"
                    "3. *Expected Behavior*: Briefly mention what should have happened at this point in the process, without discussing why it didn't happen.\n"
                    "4. *Affected Components*: List any specific components or systems involved in the error, without analyzing their role in causing the error.\n"
                    "5. *Error Context*: Provide relevant context from the steps leading up to the error, describing ONLY what occurred, not why.\n\n"
                    "Start with a brief, objective summary of the error. Use the Slack-specific formatting guidelines provided earlier. "
                    "Be concise but informative, aiming to give Yarado support staff a clear understanding of what happened, without any speculation on causes. Please ensure your entire description is generated, do not stop before completing the assignment.\n\n"
                    "Remember, your task is to describe the 'what' of the error, not the 'why'. The cause analysis will be done separately.\n\n"
                    "Provide your answer in JSON form. Reply with only the answer in JSON form and include no other commentary. "
                    "Use the following JSON structure:\n"
                    "{\n"
                    "    \"type\": \"section\",\n"
                    "    \"text\": {\n"
                    "        \"type\": \"mrkdwn\",\n"
                    "        \"text\": \"content\"\n"
                    "    }\n"
                    "}\n"
                    "Where 'content' is your formatted error description."
                )},
                {"type": "image_url",
                 "image_url": {
                     "url": "data:image/png;base64,{screenshot}".format(screenshot=screenshot)
                 }
                 }
            ]
        }
    ]

    return retry_request(client, messages)

def perform_cause_analysis(client, customer_name, process_name, preceding_steps_log, screenshot, error_description):
    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI assistant specialized in analyzing errors in Yarado's automated workflows. "
                "Your analysis needs to be formatted in JSON format and will help Yarado support staff understand and resolve issues in customer processes.\n\n"
                "Context:\n"
                "Yarado automates business processes using its 'Yarado Client' software on Azure Virtual Machines. "
                "The process '{process_name}' for customer '{customer_name}' has encountered an error. "
                "An error description has already been provided separately, so your task is to focus solely on cause analysis.\n\n"
                "Your task:\n"
                "Conduct a thorough cause analysis of the error, focusing on identifying the root cause and providing a clear causal chain of events. "
                "Pay special attention to variables, their values, and how they might relate to the error. "
                "Do not describe the error itself, as this has already been done.\n\n"
                "Input sources:\n"
                "1. Log data of the last {steps} steps:\n>>>\n{preceding_steps_log}\n>>>\n"
                "2. Screenshot of the window just before the error (attached to this message).\n"
                "3. Previous error description (for reference only, do not repeat this information):\n>>>\n{error_description}\n>>>\n\n"
                "Formatting Guidelines for Slack messages:\n"
                "- Use *asterisks* for bold text\n"
                "- Use _underscores_ for italic text\n"
                "- Use ~tildes~ for strikethrough text\n"
                "- Use `backticks` for inline code\n"
                "- Use ```triple backticks``` for code blocks\n"
                "- Use > for blockquotes\n"
                "- Use :emoji_name: for emojis\n"
                "- Use \\n for line breaks\n\n"
                "Remember, these are Slack-specific formatting rules, not standard markdown."
            ).format(
                customer_name=customer_name,
                process_name=process_name,
                steps=len(preceding_steps_log),
                preceding_steps_log=json.dumps(preceding_steps_log, indent=2),
                error_description=error_description
            )
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Provide a detailed cause analysis of the error, focusing solely on the reasons behind the error and potential solutions. "
                    "Do not describe the error itself, as this has already been done in the error description. Include the following sections:\n\n"
                    "1. *Root Cause Identification*:\n"
                    "   - Determine the fundamental reason for the error, looking beyond immediate triggers.\n"
                    "   - Explain why you believe this is the root cause, citing specific evidence from the logs or screenshot.\n\n"
                    "2. *Causal Chain and Variable Analysis*:\n"
                    "   - Provide a step-by-step breakdown of the last 10 steps leading to the error.\n"
                    "   - For each step, briefly describe what happened and how it contributed to the error.\n"
                    "   - Use the format: 'Step X.Y: [Short description of action and its impact and variables involved]'\n"
                    "   - For each step, analyze the following:\n"
                    "     - Which variables are used in this step?\n"
                    "     - What are the values of these variables?\n"
                    "     - Are these values logical in the context of the process?\n"
                    "     - Could these variable values potentially contribute to the error?\n"
                    "     - If no variables changed, do not mention anything about this.\n"
                    "   - If a step didn't directly contribute to the error, you can mention it briefly.\n\n"
                    "3. *Probability Assessment*:\n"
                    "   - If there are multiple possible causes, rank them by probability and explain your reasoning.\n"
                    "   - Consider how variable values and changes factor into this assessment.\n\n"
                    "Use the Slack-specific formatting guidelines provided earlier to structure your response clearly. "
                    "Be thorough in your analysis, but also concise and focused. Your goal is to provide Yarado support staff "
                    "with actionable insights to quickly understand and address the root cause of the error, without repeating information from the error description. Please ensure your entire analysis is generated, do not stop before completing the assignment.\n\n"
                    "Provide your answer in JSON form. Reply with only the answer in JSON form and include no other commentary. "
                    "Use the following JSON structure:\n"
                    "{\n"
                    "    \"type\": \"section\",\n"
                    "    \"text\": {\n"
                    "        \"type\": \"mrkdwn\",\n"
                    "        \"text\": \"content\"\n"
                    "    }\n"
                    "}\n"
                    "Where 'content' is your formatted cause analysis."
                )},
                {"type": "image_url",
                 "image_url": {
                     "url": "data:image/png;base64,{screenshot}".format(screenshot=screenshot)
                 }
                 }
            ]
        }
    ]

    return retry_request(client, messages)


if __name__ == '__main__':
    run_id = '36f32783-4999-41e7-8d0c-f48c1f2eeb12'
    client_name = 'Ultimoo'
    task_name = 'Email-Classification-and-Summary-main'

    try:
        client = initialize_client()
        print("Client initialized successfully.")

        log = load_log_file(run_id)
        image = load_screenshot(run_id)
        print('Input data loaded successfully.')

        point_of_failure_descr, failed_step_id = determine_point_of_failure(log)
        print('Point of failure determined.')

        preceding_steps_log = load_log_preceding_steps(log, failed_step_id, steps_to_include=10)
        print('Preceding steps loaded.')

        print('Generation of error description started...')
        error_description = generate_error_description(client, client_name, task_name, point_of_failure_descr,
                                                       preceding_steps_log, image)
        print('Error description generated successfully.')

        print('Generation of cause analysis started...')
        cause_analysis = perform_cause_analysis(client, client_name, task_name, preceding_steps_log, image,
                                                error_description)
        print('Cause analysis generated successfully.')

        print(f'\n\nError description:\n\n{error_description}\n\n')
        print(f'Cause analysis:\n\n{cause_analysis}')

    except KeyboardInterrupt:
        print("\nOperation cancelled by user. Shutting down gracefully...")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        # Perform any necessary cleanup here
        print("Script execution completed.")
