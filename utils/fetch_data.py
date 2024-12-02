import hashlib
import requests
import json
import logging
import os
from PIL import Image
from io import BytesIO
import base64
import re
from collections import defaultdict

from typing import Dict, Any
from utils.uardi_wrapper import MainTaskWrapper, StepsWrapper, ResolvedErrorWrapper
from utils.ai_utils import vectorize_text


def extract_data_from_message(message):
    PRIO_TRANSLATIONS = {
        'one': "1) Direct action required.",
        'two': "2) Action required before EoD, task needs to be completed.",
        'three': "3) Needs a look, but can wait until Yarado business hours.",
        'four': "4) No action required."
    }

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


async def load_log_file(run_id):
    endpoint = "https://api.yarado.com/v1/task-runs/{}/log".format(run_id)
    headers = {
        "X-API-KEY": get_sha256(os.getenv('YARADO_API_KEY'))
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


async def load_screenshot(run_id):
    endpoint = "https://api.yarado.com/v1/task-runs/{}/screenshot".format(run_id)
    headers = {
        "X-API-KEY": get_sha256(os.getenv('YARADO_API_KEY'))
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


def count_steps_between(log_entries, start_id, end_id):
    # Find all indices for both start_id and end_id
    start_indices = [i for i, entry in enumerate(log_entries) if entry.get('stepUuid') == start_id]
    end_indices = [i for i, entry in enumerate(log_entries) if entry.get('stepUuid') == end_id]

    if not start_indices or not end_indices:
        return 0

    # Use the last occurrence of each ID
    start_index = start_indices[-1]
    end_index = end_indices[-1]

    # Ensure start_index comes before end_index
    if start_index >= end_index:
        return 0

    count = sum(1 for entry in log_entries[start_index + 1:end_index] if 'stepUuid' in entry)
    return count


def determine_point_of_failure(log_file):
    final_failed_step_id = None
    catch_error_failed_step_id = None
    steps_between = 0

    # Parse the log file (assuming it's a JSON string)
    try:
        log_entries = json.loads(log_file)
    except json.JSONDecodeError:
        logging.error("Log file is not valid JSON.")
        return None, None, 0

    # Find the index of the TASK_FAILED event
    task_failed_index = next(
        (index for (index, entry) in enumerate(log_entries) if entry.get('eventType') == 'TASK_FAILED'), None)

    if task_failed_index is None:
        logging.error("No TASK_FAILED event found.")
        return None, None, 0

    last_completed_index = next(
        (i for i in range(task_failed_index - 1, -1, -1) if log_entries[i].get('eventType') == 'STEP_COMPLETED'), None)

    if last_completed_index is None:
        logging.warning("No STEP_COMPLETED event found before TASK_FAILED.")
        return None, None, 0

    # Traverse backwards from the TASK_FAILED event to find the last STEP_COMPLETED event
    steps_after_last_completed = []
    for i in range(task_failed_index - 1, -1, -1):
        if log_entries[i].get('eventType') == 'STEP_COMPLETED':
            # We found the last successful step before the failure
            steps_after_last_completed.append(log_entries[i])
            break
        steps_after_last_completed.append(log_entries[i])

    # Reverse the list to maintain chronological order
    steps_after_last_completed.reverse()

    if len(steps_after_last_completed) <= 1:
        return steps_after_last_completed[0].get('stepUuid'), None, 0

    # Check for skipped steps
    for i in range(1, len(steps_after_last_completed)):
        step = steps_after_last_completed[i]
        if step.get('eventType') == 'STEP_FAILED':
            failed_step = step
            final_failed_step_id = failed_step.get('stepUuid')
            break

    for entry in log_entries:
        if 'debug' in entry and 'Catching error in step' in entry['debug']:
            catch_error_failed_step_id = entry.get('stepUuid')

    if catch_error_failed_step_id and final_failed_step_id:
        # Count steps between catch error and final failure
        steps_between = count_steps_between(log_entries, catch_error_failed_step_id, final_failed_step_id)

    return final_failed_step_id, catch_error_failed_step_id, steps_between


def load_log_preceding_steps(log_file, failed_step_id, catch_error_step_id=None, steps_to_include=10):
    log_entries = json.loads(log_file)

    failed_step_index = next(
        (index for (index, entry) in enumerate(log_entries) if entry.get('stepUuid') == failed_step_id), None)

    if failed_step_index is None:
        logging.warning(f"No failed step found with the provided step ID: {failed_step_id}")
        return []

    preceding_steps = []
    unique_steps = {}
    regular_step_count = 0
    total_step_count = 0

    for i in range(failed_step_index, -1, -1):
        current_step = log_entries[i]
        event_type = current_step['eventType']

        if event_type in ['STEP_COMPLETED', 'STEP_FAILED', 'SUBTASK_COMPLETED', 'SUBTASK_FAILED', 'TASK_FAILED']:
            step_id = current_step.get('stepUuid') or f"{event_type}_{current_step.get('stepId', '')}"

            if step_id not in unique_steps:
                unique_steps[step_id] = {
                    'step': current_step,
                    'retry_count': 1,
                    'first_attempt': current_step,
                    'last_attempt': current_step
                }
                total_step_count += 1
                if event_type in ['STEP_COMPLETED', 'STEP_FAILED']:
                    regular_step_count += 1
            else:
                unique_steps[step_id]['retry_count'] += 1
                unique_steps[step_id]['first_attempt'] = current_step

            if regular_step_count >= steps_to_include:
                break

    for step_info in unique_steps.values():
        combined_step = step_info['step'].copy()
        if step_info['retry_count'] > 1:
            combined_step['retry'] = {
                'count': step_info['retry_count'],
                'first_attempt': {
                    'timestamp': step_info['first_attempt']['timestamp'],
                    'error': step_info['first_attempt'].get('error')
                },
                'last_attempt': {
                    'timestamp': step_info['last_attempt']['timestamp'],
                    'error': step_info['last_attempt'].get('error')
                }
            }
        elif 'retry' in combined_step:
            combined_step['retry']['count'] = 1

        preceding_steps.append(combined_step)

    preceding_steps.reverse()

    for step in preceding_steps:
        if 'eventType' in step:
            step.pop('eventType')
        if catch_error_step_id and 'stepUuid' in step and step['stepUuid'] == catch_error_step_id:
            step['eventType'] = 'FAILED STEP THAT CAUSED THE CATCH ERROR TRIGGER'

    return preceding_steps


def merge_log_and_uardi(preceding_steps_log, uardi_context):
    merged_steps = []
    for step in preceding_steps_log:
        merged_step = step.copy()
        step_uuid = step.get('stepUuid')
        if step_uuid in uardi_context['step_descriptions']:
            uardi_step = uardi_context['step_descriptions'][step_uuid]
            merged_step['original_ai_step_description'] = uardi_step['original_ai_step_description']
            merged_step['original_step_payload'] = uardi_step['original_step_payload']
            merged_step['stepType'] = uardi_step['type']
        merged_steps.append(merged_step)
    return merged_steps


def filter_resolved_errors(resolved_errors, max_errors=15):
    filtered_errors = sorted(resolved_errors, key=lambda x: x.get('datetime_of_resolved', ''), reverse=True)
    return [error for error in filtered_errors if is_valid_resolved_error(error)][:max_errors]


def is_valid_resolved_error(resolved_error):
    if not resolved_error:
        return False

    cause = resolved_error.get('dev_cause', '').lower()
    solution = resolved_error.get('dev_solution', '').lower()

    # Check for minimum word count
    if len(cause.split()) < 4 or len(solution.split()) < 3:
        return False

    # Check for phrases indicating lack of knowledge or unhelpful responses
    invalid_phrases = [
        'i don\'t know what happened',
        'not sure what caused this',
        'unable to determine the cause',
        'couldn\'t figure out the reason',
        'no clear explanation for this',
        'the cause remains unknown',
        'don\'t have enough information',
        'this error is a mystery',
        'need more data to understand',
        'the root cause is unclear',
        'still investigating this issue',
        'this problem is not well understood',
        'have no idea why this happened',
        'the solution is not obvious',
        'unsure how to fix this',
        'no definitive solution found',
        'need to do more research',
        'this requires further investigation',
        'still looking into this',
        'no permanent fix has been identified',
        'this is an ongoing problem',
        'haven\'t found a reliable solution',
        'the fix is only temporary',
        'not sure if this will solve it',
        'this may or may not work',
        'try restarting and see what happens',
        'just restart the process and hope',
        'restarted without understanding why',
        'randomly started working again',
        'it fixed itself somehow',
        'the error disappeared on its own',
        'didn\'t do anything and it worked',
        'no changes made but it\'s working now',
        'cannot reproduce the error',
        'unable to replicate the issue',
        'the problem seems to have resolved itself',
        'don\'t understand why it\'s working now',
        'the cause is not clear at this time',
        'need more time to investigate',
        'the solution is unclear at this point',
        'not certain about the root cause'
    ]

    if any(phrase in cause or phrase in solution for phrase in invalid_phrases):
        return False

    # Check for minimum time spent (e.g., at least 3 minutes)
    if resolved_error.get('time_spent', 0) < 3:
        return False

    return True


async def search_similar_errors(search_client, openai_client, lookup_object, failed_step_id, absolute_threshold=0.5,
                                relative_threshold=0.7):
    # Define weights for each vector field (adjust these values as needed)
    vector_weights = {
        "dev_cause_vector": 1.0,
        "dev_cause_enriched_vector": 0.6,
        "ai_context_vector": 0.0,
        "debug_pof_vector": 0.4,
        "type_pof_vector": 0.7,
        "name_pof_vector": 0.2,
        "description_pof_vector": 0.2,
        "ai_description_pof_vector": 0.7,
        "payload_pof_vector": 0.5
    }

    combined_results = defaultdict(lambda: {'score': 0, 'appearances': 0, 'max_score': 0})
    any_results_found = False

    for field, weight in vector_weights.items():
        if lookup_object.get(field.replace("_vector", "")):
            vector = await vectorize_text(client=openai_client, text=lookup_object[field.replace("_vector", "")])

            vector_query = {
                "kind": "vector",
                "vector": vector,
                "fields": field,
                "k": 10,
                "exhaustive": True
            }

            results = await search_client.search(
                search_text="*",
                vector_queries=[vector_query],
                select=["task_run_id", "task_name"]
            )

            results_found = False
            async for doc in results:
                results_found = True
                any_results_found = True
                task_run_id = doc['task_run_id']
                score = doc.get('@search.score', 0) * weight

                if task_run_id in combined_results:
                    current_score = combined_results[task_run_id]['score']
                    max_score = max(combined_results[task_run_id]['max_score'], score)
                    new_score = max_score - (max_score - current_score) * 0.5
                    combined_results[task_run_id]['score'] = new_score
                    combined_results[task_run_id]['max_score'] = max_score
                    combined_results[task_run_id]['appearances'] += 1
                else:
                    combined_results[task_run_id] = {
                        'score': score,
                        'max_score': score,
                        'appearances': 1,
                        'task_name': doc['task_name']
                    }

    if not any_results_found:
        return []

    # Convert to list and sort by score
    sorted_results = sorted(
        [{'task_run_id': k, **v} for k, v in combined_results.items()],
        key=lambda x: x['score'],
        reverse=True
    )

    # Apply thresholds
    max_score = sorted_results[0]['score'] if sorted_results else 0
    threshold_results = [
        error for error in sorted_results
        if error['score'] >= absolute_threshold and
           error['score'] >= (relative_threshold * max_score)
    ]

    if not threshold_results or len(threshold_results) == 0:
        return []

    # Filter and prioritize errors
    prioritized_errors = filter_and_prioritize_errors(threshold_results, failed_step_id)

    # Fetch full error details from ResolvedContainer
    resolved_error_wrapper = ResolvedErrorWrapper()
    task_run_ids = [error['task_run_id'] for error in prioritized_errors]
    if task_run_ids:

        full_error_details = await resolved_error_wrapper.get_resolved_errors_by_task_run_ids(task_run_ids)

        # Filter out results with unwanted substrings in dev_cause
        filtered_error_details = [
            error for error in full_error_details
            if not any(substring in error.get('dev_cause', '').lower() for substring in
                       ['unknown', 'idk', 'i dont know', 'not sure', 'unsure'])
        ]

        # Merge additional info into filtered_error_details
        error_dict = {error['task_run_id']: error for error in prioritized_errors}
        for error in filtered_error_details:
            error.update({
                'task_name': error_dict[error['task_run_id']]['task_name'],
                'similarity_score': error_dict[error['task_run_id']]['score'],
                'appearances': error_dict[error['task_run_id']]['appearances']
            })

        # Sort filtered_error_details by similarity_score
        filtered_error_details.sort(key=lambda x: x['similarity_score'], reverse=True)

        return filtered_error_details

    else:
        return []


def filter_and_prioritize_errors(unique_errors, failed_step_id, max_same_step=3, max_total=15):
    filtered_errors = []
    same_step_count = 0

    for error in unique_errors:
        if len(filtered_errors) >= max_total:
            break

        if error.get('step_id_pof') == failed_step_id:
            if same_step_count < max_same_step:
                filtered_errors.append(error)
                same_step_count += 1
        else:
            filtered_errors.append(error)

    return filtered_errors


def create_resolved_error_overview(errors, error_type='similar'):
    if not errors:
        return f"No {'similar' if error_type == 'similar' else 'historical'} resolved errors found."

    if error_type == 'similar':
        overview_title = "Similar errors have been identified:"
    else:
        overview_title = "Historical errors for this specific step:"

    resolved_error_prompt = f"{overview_title}\n\n"

    for i, error in enumerate(errors, 1):
        if error_type == 'similar':
            resolved_error_prompt += f"""
            ----------------------------- Error {i} -----------------------------
            Task: {error.get('task_name', 'Unknown')}
            Organization: {error.get('organisation_name', 'Unknown')}
            Date of Error: {error.get('datetime_of_error', 'Unknown')}
            Cause: {error.get('dev_cause', 'Not provided')}
            Solution: {error.get('dev_solution', 'Not provided')}
            Time Spent: {error.get('time_spent', 'Unknown')}
            Developer: {error.get('dev_id', 'Unknown')}
            Debug Info: {error.get('debug_pof', 'Not provided')}
            Step Type: {error.get('type_pof', 'Not provided')}
            Step Name: {error.get('name_pof', 'Not provided')}
            Description: {error.get('description_pof', 'Not provided')}
            AI Description: {error.get('ai_description_pof', 'Not provided')}
            Payload: {error.get('payload_pof', 'Not provided')}"""
        else:  # historical
            resolved_error_prompt += f"""
            ----------------------------- Error {i} -----------------------------
            Date of Error: {error.get('datetime_of_error', 'Unknown')}
            Cause: {error.get('dev_cause', 'Not provided')}
            Solution: {error.get('dev_solution', 'Not provided')}
            Time Spent: {error.get('time_spent', 'Unknown')}
            Developer: {error.get('dev_id', 'Unknown')}"""

            # Add AI-generated content if available (for historical errors)
            if all(key in error for key in ['ai_description', 'ai_cause', 'supporter_feedback', 'supporter_rate']):
                resolved_error_prompt += f"""
            AI Description: {error['ai_description']}
            AI Cause Analysis: {error['ai_cause']}
            AI Supporter Feedback: {error['supporter_feedback']}
            AI Supporter Rating: {error['supporter_rate']}/5"""

        resolved_error_prompt += "\n"

    if error_type == 'similar':
        resolved_error_prompt += """
        Note to AI assistant analyzing errors in Yarado's automated workflows:
        1. Use the information from similar errors to inform your analysis, but do not rely solely on these past instances.
        2. Consider patterns in the types of errors, their causes, and the contexts in which they occur.
        3. Pay attention to similarities in step types, names, and payloads between the current error and these similar errors.
        4. Use this information to enrich your understanding of the current error context, not to directly explain its cause.
        5. If you notice recurring patterns or frequent similarities, mention these in your analysis.
        6. Remember that while these errors are similar, each instance is unique and should be analyzed in its specific context.
        7. Use these similar errors as a supplement to your own analysis and the historical errors for the specific step.
        """
    else:  # historical
        resolved_error_prompt += """
        Note to AI assistant specialized in analyzing errors in Yarado's automated workflows:
        1. Prioritize the information from these historical errors as they occurred at the exact same step and are highly relevant to the current issue.
        2. Pay close attention to recurring causes, solutions, or restart information across these historical errors. If certain patterns appear repeatedly, they are likely to be significant and should be given more weight in your analysis.
        3. While historical patterns are crucial, always combine them with your own independent analysis of the current error. Your unique insights are valuable and may uncover new aspects not present in historical data.
        4. Use the 'Cause' and 'Solution' from historic errors to inform your analysis, but remember that they come from human developers who can make mistakes. Cross-reference these with your own findings and the current error context.
        5. If you see conflicting information between historical errors and your current analysis, acknowledge both perspectives and explain your reasoning for favoring one over the other.
        6. Use the developer information to identify team members with experience in similar issues. Consider suggesting collaboration with these developers if the current error seems particularly complex or similar to past instances they've resolved.
        7. Analyze the AI-generated descriptions and cause analyses from past errors, along with any supporter feedback and ratings. Use this to gauge the effectiveness of past solutions and to inform your current analysis.
        8. Compare the most recent error's details (payload, debug info) with the current error to identify any changes or patterns that might be relevant.
        9. Consider the time spent on resolving these historical errors as an indicator of the issue's complexity. If certain types of errors consistently take longer to resolve, this may inform your assessment of the current error's severity or complexity.
        10. While relying on historical patterns, remain open to the possibility of new or evolving issues. Your analysis should balance historical insights with fresh perspectives on the current error.
        """

    return resolved_error_prompt


def create_combined_error_overview(historical_errors, similar_errors):
    historical_errors_count = len(historical_errors)
    max_similar_errors = min(15, 30 - historical_errors_count)

    # Combine historical errors with a cap of 30 total errors
    final_historical_errors = historical_errors[:30]
    final_similar_errors = similar_errors[:max_similar_errors]

    # Create separate overviews
    historical_error_overview = create_resolved_error_overview(final_historical_errors, error_type='historical')
    similar_error_overview = create_resolved_error_overview(final_similar_errors, error_type='similar')

    return historical_error_overview, similar_error_overview


def find_json_by_key_value(json_list, key, value):
    for item in json_list:
        if isinstance(item, dict):  # Check if the item is a dictionary
            if item.get(key) == value:
                return item
        else:
            logging.warning(f"Non-dictionary item found in json_list: {item}")
    return None


async def get_uardi_context(organisation_name: str, task_name: str, step_ids: list[str], failed_step_id: str) -> Dict[
    str, Any]:
    main_task_container = MainTaskWrapper()
    steps_container = StepsWrapper()
    resolved_error_container = ResolvedErrorWrapper()

    task_data = await main_task_container.get_main_task(organisation_name, task_name)

    if not task_data:
        return {
            "main_task_data": None,
            "step_descriptions": {}
        }

    organisation_id = task_data.get('organisation_id')

    if not organisation_id:
        return {
            "main_task_data": task_data,
            "step_descriptions": {}
        }

    step_descriptions = {}
    for step_id in step_ids:
        step_data = await steps_container.get_step(organisation_id, step_id)
        if step_data:
            step_descriptions[step_id] = {
                "original_ai_step_description": step_data.get('ai_description', 'Unknown step description'),
                "original_step_payload": step_data.get('payload', {}),
                "type": step_data.get('type')
            }

    resolved_errors = await resolved_error_container.get_resolved_errors(organisation_id, failed_step_id)

    context = {
        "main_task_data": task_data,
        "step_descriptions": step_descriptions,
        "resolved_errors": resolved_errors
    }

    return context


# async def test_main():
#     organisation_name = "MijZo"
#     task_name = "Financiering_Opschaling-11_days"
#     run_id = "573f7c42-ef40-454c-816f-b5e802154472"
#     log_file = await load_log_file(run_id)
#     screenshot = await load_screenshot(run_id)
#
#     if log_file is None or log_file == "INVALID_JSON":
#         raise ValueError("Unable to fetch the log file or invalid JSON format")
#     if screenshot is None or screenshot == "INVALID_IMAGE":
#         raise ValueError("Unable to fetch the screenshot or invalid image format")
#
#     logging.info('Input data loaded successfully.')
#
#     failed_step_id, catch_error_step_id, steps_between = determine_point_of_failure(log_file)
#     if failed_step_id is None:
#         raise ValueError("Could not determine the point of failure from the log file")
#
#     preceding_steps_log = load_log_preceding_steps(
#         log_file, failed_step_id,
#         catch_error_step_id=catch_error_step_id,
#         steps_to_include=10 + steps_between
#     )
#     if not preceding_steps_log:
#         raise ValueError(f"No preceding steps found for failed_step_id: {failed_step_id}")
#
#     logging.info('Log analysis completed.')
#
#     uardi_context = await get_uardi_context(
#         organisation_name=organisation_name, task_name=task_name,
#         step_ids=[step['stepUuid'] for step in preceding_steps_log if 'stepUuid' in step],
#         failed_step_id=failed_step_id
#     )
#
#     print(uardi_context)
#
#
# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(test_main())