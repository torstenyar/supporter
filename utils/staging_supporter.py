import asyncio
import json
import logging
import time
import random
from utils.uardi_integration import get_uardi_context
from utils.azure_openai_client import initialize_openai_client
from utils.supporter import (
    load_log_file, load_screenshot, determine_point_of_failure, extract_variable_changes, extract_relevant_variables,
    generate_textual_overview, convert_to_slack_format
)
import openai
from azure.search.documents.aio import SearchClient
from azure.core.credentials import AzureKeyCredential
import os
from dotenv import load_dotenv
from utils.uardi_wrapper import ResolvedErrorWrapper
from collections import defaultdict

load_dotenv()

client = initialize_openai_client()

logging.basicConfig(level=logging.WARNING)


def load_log_preceding_steps(log_file, failed_step_id, steps_to_include=10):
    log_entries = json.loads(log_file)

    failed_step_index = next(
        (i for i in reversed(range(len(log_entries)))
         if log_entries[i].get('stepUuid') == failed_step_id
         and log_entries[i]['eventType'] == 'STEP_FAILED'),
        None
    )

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
        if 'eventType' in preceding_steps:
            step.pop('eventType')

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


async def vectorize_text(text):
    response = client.embeddings.create(
        model="text-embedding-3-large",
        input=text,
        dimensions=3072
    )
    return response.data[0].embedding


async def search_similar_errors(lookup_object, failed_step_id, absolute_threshold=0.5, relative_threshold=0.7):
    search_client = SearchClient(
        endpoint=os.getenv("SEARCH_ENDPOINT"),
        index_name=os.getenv("SEARCH_INDEX_NAME"),
        credential=AzureKeyCredential(os.getenv("SEARCH_API_KEY"))
    )

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
            print(f"Searching field: {field}")
            vector = await vectorize_text(lookup_object[field.replace("_vector", "")])

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

            if not results_found:
                print(f"No results found for field: {field}")

    if not any_results_found:
        print("No results found for any field.")
        return []

    # Convert to list and sort by score
    sorted_results = sorted(
        [{'task_run_id': k, **v} for k, v in combined_results.items()],
        key=lambda x: x['score'],
        reverse=True
    )

    print(f"Total similar errors found: {len(sorted_results)}")

    # Apply thresholds
    max_score = sorted_results[0]['score'] if sorted_results else 0
    threshold_results = [
        error for error in sorted_results
        if error['score'] >= absolute_threshold and
           error['score'] >= (relative_threshold * max_score)
    ]

    print(f"Errors after applying thresholds: {len(threshold_results)}")

    if not threshold_results or len(threshold_results) == 0:
        return []

    # Filter and prioritize errors
    prioritized_errors = filter_and_prioritize_errors(threshold_results, failed_step_id)

    print(f"Prioritized errors: {len(prioritized_errors)}")

    # Fetch full error details from ResolvedContainer
    resolved_error_wrapper = ResolvedErrorWrapper()
    task_run_ids = [error['task_run_id'] for error in prioritized_errors]
    if task_run_ids:
        print(f"Task run IDs to fetch: {task_run_ids}")

        full_error_details = await resolved_error_wrapper.get_resolved_errors_by_task_run_ids(task_run_ids)

        print(f"Full error details fetched: {len(full_error_details)}")

        # Filter out results with unwanted substrings in dev_cause
        filtered_error_details = [
            error for error in full_error_details
            if not any(substring in error.get('dev_cause', '').lower() for substring in
                       ['unknown', 'idk', 'i dont know', 'not sure', 'unsure'])
        ]

        print(f"Errors after filtering dev_cause: {len(filtered_error_details)}")

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


def retry_request_openai(client, messages, model="gpt-4o-2024-08-06", max_retries=5, initial_timeout=1, max_timeout=60,
                         max_tokens=4096):
    for attempt in range(max_retries):
        try:
            print(f"Attempt {attempt + 1} of {max_retries}...")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                timeout=90,
                seed=42
            )
            print(f"Request successful on attempt {attempt + 1}")
            ai_generated_content = response.choices[0].message.content
            return ai_generated_content
        except openai.OpenAIError as e:
            if attempt == max_retries - 1:
                print(f"Max retries reached. Last error: {e}")
                logging.error(f"Max retries reached. Last error: {e}")
                error_message = f":warning: Error: OpenAI did not respond successfully after multiple attempts. \n\nLast error: \n```{str(e)}```\n\nPlease try again later."
                return error_message

            wait_time = min(initial_timeout * (2 ** attempt) + random.uniform(0, 1), max_timeout)
            print(f"Attempt {attempt + 1} failed. Retrying in {wait_time:.2f} seconds. Error: {e}")
            logging.warning(f"Attempt {attempt + 1} failed. Retrying in {wait_time:.2f} seconds. Error: {e}")
            time.sleep(wait_time)

    return ":warning: Unexpected error occurred during API request."


async def generate_error_context(client, customer_name, process_name, steps_log, screenshot,
                                 uardi_context, historical_error_overview):
    # Remove any sensitive information from uardi_context
    safe_uardi_context = uardi_context.copy()

    if 'main_task_data' in safe_uardi_context:
        safe_uardi_context['main_task_data'] = {
            k: v for k, v in safe_uardi_context['main_task_data'].items()
            if k not in ['id', 'organisation_id', 'overall', 'creation_date', 'last_updated', 'main_task_structure',
                         'step_descriptions', 'process_description', 'organisation_profile_last_updated', 'stats',
                         'last_request_date_time', '_rid', '_self', '_etag', '_attachments', '_ts']
        }

    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI assistant designed to help Yarado support staff understand the technical context of errors in automated workflows. "
                "Your audience consists of highly technical Yarado employees who are familiar with automation processes and systems.\n\n"
                "Context:\n"
                "The process '{process_name}' was developed for the customer '{customer_name}'. Your task is to provide a clear, concise, and technically focused description of the error context.\n\n"
                "Input sources the user will provide:\n"
                "1. Historical error information: Data about errors that have occurred at this specific step in the past.\n"
                "2. UARDI Data Structure:\n"
                "- The 'organisation_profile' field contains information about the client's business domain.\n"
                "- The 'ai_task_summary' field provides an overview of the task's purpose and workflow.\n"
                "- The 'tasks' key provides a hierarchical structure of the main task and its subtasks. For each task:\n"
                "  * 'task_name': The name of the task or subtask\n"
                "  * 'num_steps': Total number of steps in the task\n"
                "  * 'num_subtasks': Number of subtasks within this task\n"
                "  * 'loop_start' and 'loop_end': If present, indicate the step range of a loop within the task\n"
                "  * 'num_variables' and 'num_secrets': Count of variables and secrets used\n"
                "  * 'step_types': A breakdown of the types of steps in the task\n"
                "  * 'subtasks': A nested object containing similar information for each subtask\n"
                "3. Log Data Structure:\n"
                "   The log data contains a series of step entries, each representing a specific action in the workflow. Each step entry includes:\n"
                "   3A. Run-specific information:\n"
                "   - 'timestamp': The time when the step was executed.\n"
                "   - 'stepUuid': A unique identifier for the step.\n"
                "   - 'stepId': The step's position in the workflow (e.g., '27,1').\n"
                "   - 'stepType': The type of action performed (e.g., 'Function', 'HttpRequest', 'Condition').\n"
                "   - 'name': A descriptive name of the step.\n"
                "   - 'executionTime': Time taken to execute the step (in milliseconds).\n"
                "   - 'loop': Indicates which iteration of a loop this step is part of, if applicable.\n"
                "   - 'task': The file path of the task being executed.\n"
                "   - 'depth': The nesting level of the step within the workflow.\n"
                "   - 'changedVariables': A list of variables that were modified during this step, including their old and new values.\n"
                "   - 'debug': Detailed debugging information about the step's execution.\n"
                "   3B. Task-run-independent information:\n"
                "   - 'original_ai_step_description': An AI-generated description of what the step is supposed to do, independent of any specific run.\n"
                "   - 'original_step_payload': The original configuration or parameters for the step as defined in the task file.\n"
                "   These task-run-independent fields provide context about the intended behavior of each step, which is crucial when comparing against what actually happened during execution.\n"
                "4. Screenshot: An image of the Azure VM screen at the moment the error occurred. This screenshot is a unique feature of the Yarado Client and provides crucial visual context. It can reveal:\n"
                "   - The state of the application or website being interacted with\n"
                "   - Any visible error messages or unexpected UI states\n"
                "   - The presence of pop-ups or system notifications\n"
                "   - The overall desktop environment and any relevant background processes\n"
                "   - Timestamps or other temporal information visible on the screen\n"
                "   The screenshot should be analyzed in conjunction with the log data to provide a more comprehensive understanding of the error context. It may reveal issues not apparent in the logs alone, such as network disconnections, unexpected application behavior, or system-level issues.\n\n"
                "Structure your response as follows:\n"
                "1. Task Technical Overview: Briefly describe the high-level technical flow of the main task (derive this from the summary, and only the main object in the task JSON object - not the nested subtasks). Focus on:\n"
                "   - Systems and websites involved\n"
                "   - Types of data processed\n"
                "   - Key data processing steps\n"
                "   - RPA, AI, APIs or integration points (if present)\n"
                "   Present this information densely, assuming high technical knowledge of the audience. Never mention the number of steps in the task in this section.\n"
                "2. Error Location, Context, and Historical Overview: Specify the exact step coordinate - how this relates to maintask/subtask and loop. In the point of failure description you will see the task in which the step failed - whether it is a subtask step or a maintask step, relate this to the corresponding object in the 'tasks' object, in which loop the process was (if we were in a loop), and task where the error occurred, and indicate how far the process probably was. Include step coordinates and indicate the error's position relative to the overall process flow. This should follow logically after the previous part on task technical overview, indicate how it relates to this part and where in the flow this error occurred.\n"
                "When analyzing the error location:\n"
                " 2.1. Identify the task or subtask where the error occurred based on the 'task' field in the log entry\n"
                " 2.2. Note the step coordinates (e.g., '27,1') and relate it to the task structure\n"
                " 2.3. Determine if the error occurred within a loop by checking the 'loop_start' and 'loop_end' values\n"
                " 2.4. If in a loop, calculate how far into the loop the error occurred\n"
                " 2.5. Estimate the overall progress of the task based on the error's step number relative to 'num_steps'\n"
                " 2.6. Incorporate historical error information:\n"
                "      - Describe how frequently errors have occurred at this specific step (note you will see at max 30 historical errors)\n"
                "      - Identify any patterns in the timing or conditions under which these errors typically occur\n"
                "      - Mention developers who have frequently addressed similar issues in the past\n"
                "      - Briefly note how long these types of errors typically take to resolve (based on historical data)\n"
                "      - The more shared findings between historical errors, the more confident you can be in your observations\n"
                " 2.7. If relevant, mention insights from similar errors, noting that they are ordered by similarity but may not be from the exact same step\n"
                "This information is crucial for providing accurate context about where in the process flow the error occurred and how it relates to past issues.\n"
                "3. Observed Behavior: Describe the observable technical facts from the log and screenshot. Pay special attention to any discrepancies between what the logs indicate and what is visible in the screenshot.\n"
                "4. Expected Behavior: Briefly mention the expected technical outcome at this point in the process.\n\n"
                "Important:\n"
                "- Focus solely on technical aspects relevant to troubleshooting.\n"
                "- Never explain the benefits of automation or why the process was automated.\n"
                "- Avoid business jargon; stick to technical terminology.\n"
                "- Never speculate on causes or offer analysis.\n"
                "- Use plain text formatting without special structuring.\n"
                "- Integrate observations from the screenshot throughout your analysis, especially in the Observed Behavior section.\n"
                "- Note that the log data does not contain explicit status indicators (such as 'success' or 'failure') for each step. You must infer the outcome of each step based on the available information.\n"
                "- When discussing step outcomes, clearly explain your reasoning and the evidence you're using to draw conclusions.\n"
                "- Analyze the screenshot in detail and relate your observations to the log data and UARDI context. Look for visual cues that might provide additional insights into the error context.\n"
                "- When using historical error information, focus on patterns and frequencies, not on specific causes or solutions.\n"
                "- Treat similar errors as supplementary information, using them to enrich your understanding but prioritizing historical errors for this specific step."
            ).format(
                customer_name=customer_name,
                process_name=process_name
            )
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Hi GPT, thoroughly analyse your system instructions and remember to follow them closely. Remember to act as a Yarado-employee and thus as a colleague of the one requesting this task.\n\n"
                    "Generate a technical context overview for the error based on these inputs:\n\n"
                    "1. Historical error information:\n>>>\n{historical_error_overview}\n>>>\n"
                    "2. Task and organization information:\n>>>\n{uardi_context}\n>>>\n"
                    "3. Log data of the last {steps} steps:\n>>>\n{steps_log}\n>>>\n"
                    "4. Screenshot of the window just before the error (attached).\n\n"
                    "Provide a comprehensive technical context that will help Yarado support staff quickly understand the task's technical flow, "
                    "where in the process the error occurred, and what was being attempted from a systems and data perspective. "
                    "Focus on technical details that are directly relevant to troubleshooting the error. "
                    "Make sure to incorporate insights from the screenshot throughout your analysis, particularly in describing the observed behavior."
                    "Remember that this section purely focuses on giving context about the error - NEVER indicate a potential cause or solution in this section.\n\n"
                ).format(
                    steps=len(steps_log) - 1,
                    steps_log=json.dumps(steps_log, indent=2),
                    uardi_context=json.dumps(safe_uardi_context['main_task_data'], indent=2),
                    historical_error_overview=historical_error_overview
                )},
                {"type": "image_url",
                 "image_url": {
                     "url": f"data:image/png;base64,{screenshot}"
                 }
                 }
            ]
        }
    ]

    return retry_request_openai(client, messages)


async def perform_cause_analysis(client, customer_name, process_name, steps_log, screenshot,
                                 uardi_context, ai_generated_error_context, historical_error_overview, similar_error_overview):
    # Remove any sensitive information from uardi_context
    safe_uardi_context = uardi_context.copy()
    if 'main_task_data' in safe_uardi_context:
        safe_uardi_context['main_task_data'] = {
            k: v for k, v in safe_uardi_context['main_task_data'].items()
            if k not in ['id', 'organisation_id', 'overall', 'tasks', 'creation_date', 'last_updated',
                         'main_task_structure', 'process_description', 'organisation_profile_last_updated', 'stats',
                         'last_request_date_time', '_rid', '_self', '_etag', '_attachments', '_ts']
        }

    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI assistant specialized in analyzing errors in Yarado's automated workflows. "
                "Your audience consists of highly technical Yarado employees who are experts in automation processes and systems.\n\n"
                "Context:\n"
                "The process '{process_name}' for customer '{customer_name}' has encountered an error. "
                "An error description and context will be provided by the user. Your task is to perform a detailed cause analysis.\n\n"
                "Input sources the user will provide:\n"
                "1. Historical Error Information: Data about errors that have occurred at this specific step in the past.\n"
                "2. Similar Error Information: Data about errors that are similar to the current one, found using a RAG model and ordered by similarity.\n"
                "3. AI-Generated Error Context: This is a comprehensive error description created by another AI model. It provides an overview of the task, the error location, observed behavior, and expected behavior. Use this as a starting point for your analysis, never repeat any of its content. Your analysis will be added as a subsequent section to this provided section.\n"
                "4. UARDI Data Structure:\n"
                "   - The 'organisation_profile' field contains information about the client's business domain.\n"
                "   - The 'ai_task_summary' field provides an overview of the task's purpose and workflow.\n"
                "5. Log Data Structure:\n"
                "   The log data contains a series of step entries, each representing a specific action in the workflow. Each step entry includes:\n"
                "   5A. Run-specific information:\n"
                "   - 'timestamp': The time when the step was executed.\n"
                "   - 'stepUuid': A unique identifier for the step.\n"
                "   - 'stepId': The step's position in the workflow (e.g., '27,1').\n"
                "   - 'stepType': The type of action performed (e.g., 'Function', 'HttpRequest', 'Condition').\n"
                "   - 'name': A descriptive name of the step.\n"
                "   - 'executionTime': Time taken to execute the step (in milliseconds).\n"
                "   - 'loop': Indicates which iteration of a loop this step is part of, if applicable.\n"
                "   - 'task': The file path of the task being executed.\n"
                "   - 'depth': The nesting level of the step within the workflow.\n"
                "   - 'changedVariables': A list of variables that were modified during this step, including their old and new values.\n"
                "   - 'debug': Detailed debugging information about the step's execution.\n"
                "   5B. Task-run-independent information:\n"
                "   - 'original_ai_step_description': An AI-generated description of what the step is supposed to do, independent of any specific run.\n"
                "   - 'original_step_payload': The original configuration or parameters for the step as defined in the task file.\n"
                "   These task-run-independent fields provide context about the intended behavior of each step, which is crucial when comparing against what actually happened during execution.\n"
                "6. Screenshot: An image of the Azure VM screen at the moment the error occurred. This screenshot is a unique feature of the Yarado Client and provides crucial visual context. It can reveal:\n"
                "   - The state of the application or website being interacted with\n"
                "   - Any visible error messages or unexpected UI states\n"
                "   - The presence of pop-ups or system notifications\n"
                "   - The overall desktop environment and any relevant background processes\n"
                "   - Timestamps or other temporal information visible on the screen\n"
                "   The screenshot should be analyzed in conjunction with the log data to provide a more comprehensive understanding of the error context. It may reveal issues not apparent in the logs alone, such as network disconnections, unexpected application behavior, or system-level issues.\n\n"
                "#######"
                "OUTPUT:"
                "Structure your response as follows:\n"
                "5. Historical and Similar Error Causes Comparison:\n"
                "   - Briefly compare the current error with historical errors causes at this step. You are encouraged to repeat/quote earlier causes written by developers.\n"
                "   - Highlight any recurring patterns or notable differences in historical errors causes.\n"
                "   - Discuss how similar errors (from the RAG model) relate to the current error, noting that they may not be from the exact same step.\n"
                "   - Mention developers who have frequently addressed similar or historical issues, if this information is available. Only tell this if it is a obvious one, and the historical error solver weigh much heavier than a similar error solver.\n"
                "   - Compare the visual state in the current screenshot with any descriptions of visual states in historical or similar errors.\n\n"
                "6. Causal Chain Analysis:\n"
                "   - Provide a concise step-by-step breakdown of events leading to the error.\n"
                "   - For each relevant step, describe its action, impact, and any variable changes. Use the 'original_ai_step_description' for context.\n"
                "   - Use the format: 'Step X.Y: [Concise description of action, impact, and key variables]'\n"
                "   - Focus on variable values, their logic in the process context, and potential contribution to the error.\n"
                "   - Draw connections between steps to illustrate the causal progression.\n"
                "   - Pay special attention to steps preceding the error. Analyze whether these steps completed successfully and as expected.\n"
                "   - Consider environmental factors that might affect step execution, such as page loading issues or data availability.\n"
                "   - If relevant, compare the current causal chain with patterns observed in historical errors at similar steps.\n"
                "   - Explicitly state your reasoning for inferring the success or failure of each step, as there are no explicit status indicators in the log data.\n"
                "   - Relate your observations from the log data to what you see in the screenshot, explaining any correlations or discrepancies.\n"
                "7. Root Cause and Technical Impact:\n"
                "   - Determine the fundamental reason for the error, looking beyond the immediate error step.\n"
                "   - Consider whether the root cause lies in earlier steps, data preparation, or environmental factors.\n"
                "   - Explain your reasoning, citing specific evidence from logs, screenshot, UARDI data, and historical data. It is very important for you to explain your conclusion/reasoning.\n"
                "   - If historical data shows similar root causes for this step, discuss how the current root cause aligns with or differs from these historical patterns.\n"
                "   - Explain how the root cause affects the overall process from a technical perspective.\n"
                "   - Discuss any potential ripple effects on other systems or processes.\n"
                "   - If available, mention how frequently this root cause has occurred historically and any notable trends.\n"
                "   - Consider whether intermittent issues (like page loading problems) could be contributing to the error.\n"
                "   - Analyze how the screenshot supports or challenges your root cause hypothesis, providing detailed observations.\n"
                "8. Probability Analysis (if applicable):\n"
                "   - ONLY generate this section if multiple distinct causes are highly plausible!\n"
                "   - If multiple causes are highly plausible, rank them by likelihood and explain your reasoning.\n"
                "   - Consider how variable values and changes factor into this assessment.\n"
                "   - Incorporate historical error frequencies to support your probability analysis, if relevant.\n"
                "   - Explain how visual evidence from the screenshot influences your probability assessment of different causes.\n"
                "Important:\n"
                "- Focus solely on cause analysis. NEVER provide resolution steps or recommendations.\n"
                "- While analyzing, consider both the immediate error and potential issues in preceding steps or the environment.\n"
                "- Pay attention to data dependencies between steps and whether all necessary data was properly loaded or prepared.\n"
                "- Be aware that the visible error step may not always be the true root cause of the problem.\n"
                "- Be concise in your explanations while still providing necessary technical details.\n"
                "- Use technical terminology appropriate for expert Yarado staff.\n"
                "- Ensure your analysis logically follows and builds upon the provided error context.\n"
                "- Do not repeat information from the error context unless directly relevant to cause analysis.\n"
                "- Integrate observations from the screenshot throughout your analysis, especially when discussing the causal chain and root cause.\n"
                "- Use plain text formatting without special structuring.\n"
                "- When using historical error information, compare causes with your own analysis, but never discuss past solutions.\n"
                "- Prioritize insights from historical errors over similar errors, as they are specific to this exact step.\n"
                "- Use similar errors to enrich your understanding, but treat them as supplementary to historical errors.\n"
                "- If historical data is limited or not available for this specific error, clearly state this and focus more on the current error analysis and similar errors.\n"
                "- Note that the log data does not contain explicit status indicators (such as 'success' or 'failure') for each step. You must infer the outcome of each step based on the available information.\n"
                "- When discussing step outcomes, clearly explain your reasoning and the evidence you're using to draw conclusions.\n"
                "- Analyze the screenshot in detail and relate your observations to the log data and UARDI context. Look for visual cues that might provide additional insights into the error context.\n"
                "- Remember, you're seeing up to 30 historical errors. The more shared findings between these errors, the more confident you can be in your observations."
            ).format(
                customer_name=customer_name,
                process_name=process_name
            )
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Hi GPT, thoroughly analyse your system instructions and remember to follow them closely. Remember to act as a Yarado-employee and thus as a colleague of the one requesting this task.\n\n"
                    "Perform a detailed cause analysis based on the following inputs:\n\n"
                    "1. Historical error information:\n>>>\n{historical_error_overview}\n>>>\n"
                    "2. Similar error information:\n>>>\n{similar_error_overview}\n>>>\n"
                    "3. Previously generated error context:\n>>>\n{ai_generated_error_context}\n>>>\n"
                    "4. Log data of (up to) the last 10 steps:\n>>>\n{steps_log}\n>>>\n"
                    "5. Task and organization information:\n>>>\n{uardi_context}\n>>>\n"
                    "6. Screenshot of the window just before the error (attached).\n\n"
                    "When using the historical and similar error information:\n"
                    "- Prioritize information from historical errors as they are specific to this exact step.\n"
                    "- Use similar errors to enrich your understanding, but treat them as supplementary to historical errors.\n"
                    "- Do not simply rely on a single historic error. Use your own chain of thoughts and findings alongside the historical data.\n"
                    "- Remember that the 'Cause' and 'Solution' from historical errors are not absolute truths. They come from our developers, who can also make mistakes.\n"
                    "- Use the developer information to identify team members with experience in similar issues, but focus on the technical aspects rather than individuals.\n"
                    "- Consider AI-generated descriptions and cause analyses from past errors, along with any supporter feedback and ratings, to gauge the effectiveness of past analyses.\n"
                    "- You may compare the most recent error payload and debug information with the current error to identify changes or patterns, if relevant.\n"
                    "- Remember, you're seeing up to 30 historical errors. The more shared findings between these errors, the more confident you can be in your observations.\n\n"
                    "Provide a comprehensive cause analysis that logically follows and builds upon the error context. "
                    "Focus on identifying the root cause and detailing the causal chain of events. "
                    "Remember, your analysis is for the Yarado support staff to understand the issue effectively. "
                    "NEVER provide any resolution steps or recommendations in this analysis. "
                    "Make sure to incorporate insights from the AI-generated error context, historical errors, similar errors, and the screenshot throughout your analysis."
                ).format(
                    ai_generated_error_context=ai_generated_error_context,
                    steps_log=json.dumps(steps_log, indent=2),
                    uardi_context=json.dumps(safe_uardi_context['main_task_data'], indent=2),
                    historical_error_overview=historical_error_overview,
                    similar_error_overview=similar_error_overview
                )},
                {"type": "image_url",
                 "image_url": {
                     "url": f"data:image/png;base64,{screenshot}"
                 }
                 }
            ]
        }
    ]

    return retry_request_openai(client, messages)


async def summarize_ai_cause(client, ai_cause):
    messages = [
        {
            "role": "system",
            "content": """You are an AI assistant tasked with transforming detailed AI generated error cause analyses into brief (most of the time one line), concise human like cause statements. Your transformation should mimic the style of human-written causes, typically one or two sentences long. Only output the transformation and nothing else. Never mention things a human could not know (for example historical errors are not known to the human developers). You should really act as if you are the developer writing this one/two liner. Focus on '6. Root Cause and Technical Impact:' as here the root cause is stated which is most oftenly written directly by a developer.
            
            
            Here are some examples of the style and brevity we're aiming for:

1. OneDrive automatically signed out, and the system's failsafe mechanism successfully detected this event.
2. A different pop-up button within the Softpak application has been modified.
3. The individual we were supposed to verify was not found in the Relian database.
4. The web page experienced a delay in loading.
5. A problem has been detected with KVS.
6. The robot's operation either proceeded too quickly, or the web page responded slowly.
7. The expected session cookie was not retrieved.

Learn from these examples and ensure your output is of similar length (usually one line) and conciseness."""
        },
        {
            "role": "user",
            "content": f"{ai_cause}"
        }
    ]

    summary = retry_request_openai(client, messages, model='gpt-4o-mini')
    return summary


async def generate_restart_information_and_solution(client, error_context, cause_analysis, historical_error_overview, similar_error_overview):
    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI assistant specialized in providing restart information and solution recommendations for errors in Yarado's automated workflows. "
                "Your audience consists of highly technical Yarado employees who are experts in automation processes and systems.\n\n"
                "Context:\n"
                "An error has occurred in a Yarado automated process. You have been provided with the error context, cause analysis, and historical and similar error information. "
                "Your task is to generate restart information and solution recommendations.\n\n"
                "Input sources:\n"
                "1. Error Context: A comprehensive description of the error, including its location and observed behavior.\n"
                "2. Cause Analysis: A detailed analysis of the root cause and causal chain leading to the error.\n"
                "3. Historical Error Information: Data about errors that have occurred at this specific step in the past.\n"
                "4. Similar Error Information: Data about errors that are similar to the current one, found using a RAG model and ordered by similarity.\n\n"
                "Structure your response as follows:\n"
                "1. Restart Information:\n"
                "   - Base this section SOLELY on the Historical Error Information.\n"
                "   - Do NOT use Similar Error Information for restart recommendations.\n"
                "   - Clearly state the step (and loop, if applicable) from which the process can be restarted.\n"
                "   - Explain the reasoning behind the restart point, citing specific evidence from the historical errors.\n"
                "   - Mention the source (e.g., specific historical error entry) that supports your restart recommendation.\n"
                "   - If no historical errors are found, clearly state this and provide a cautious inference based on the error context and cause analysis.\n"
                "   - If inferring a restart point without historical data, include a clear disclaimer about the uncertainty of this recommendation.\n\n"
                "2. Solution Recommendations:\n"
                "   - Provide recommendations on how to prevent or fix this error in the future.\n"
                "   - Use insights from Historical Error Information, Similar Error Information, and your general knowledge of automation processes.\n"
                "   - Prioritize solutions that have been successful in historical errors.\n"
                "   - Consider solutions from similar errors, but clearly indicate when a recommendation comes from a similar (not identical) error.\n"
                "   - Provide a mix of short-term fixes and long-term improvements where applicable.\n"
                "   - Explain the reasoning behind each recommendation.\n\n"
                "Important guidelines:\n"
                "- For Restart Information, use ONLY Historical Error Information. Similar errors may be from different steps and could lead to incorrect restart points.\n"
                "- Be explicit about the source and confidence level of each piece of information or recommendation.\n"
                "- Use technical language appropriate for Yarado staff, but ensure clarity in your explanations.\n"
                "- If historical data is limited or not available, clearly state this and adjust your confidence level accordingly.\n"
                "- When using information from similar errors in the Solution section, clearly distinguish it from information about the exact error step.\n"
                "- Avoid repeating information from the error context or cause analysis unless directly relevant to restart or solution recommendations.\n"
                "- Remember, you're seeing up to 30 historical errors. The more shared findings between these errors, the more confident you can be in your recommendations.\n"
                "- Use plain text formatting without special structuring.\n"
                "- Be concise but thorough in your explanations."
            )
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Hi GPT, thoroughly analyse your system instructions and remember to follow them closely. Remember to act as a Yarado-employee and thus as a colleague of the one requesting this task.\n\n"
                    "Generate restart information and solution recommendations based on the following inputs:\n\n"
                    "1. Error Context:\n>>>\n{error_context}\n>>>\n"
                    "2. Cause Analysis:\n>>>\n{cause_analysis}\n>>>\n"
                    "3. Historical Error Information:\n>>>\n{historical_error_overview}\n>>>\n"
                    "4. Similar Error Information:\n>>>\n{similar_error_overview}\n>>>\n"
                    "Remember:\n"
                    "- For Restart Information, use ONLY the Historical Error Information. Do not use Similar Error Information for this section.\n"
                    "- For Solution Recommendations, you can use insights from all sources, including your general knowledge.\n"
                    "- Clearly indicate the source and confidence level of each piece of information or recommendation.\n"
                    "- Be explicit about which information comes from historical errors (same step) vs. similar errors (potentially different steps).\n"
                    "- If there's limited or no historical data, clearly state this and adjust your recommendations and confidence accordingly.\n\n"
                    "Provide comprehensive restart information and solution recommendations that will help Yarado support staff effectively address and prevent this error in the future."
                ).format(
                    error_context=error_context,
                    cause_analysis=cause_analysis,
                    historical_error_overview=historical_error_overview,
                    similar_error_overview=similar_error_overview
                )}
            ]
        }
    ]

    return retry_request_openai(client, messages)


async def combine_and_refine_analysis(client, error_description, cause_analysis, restart_and_solution):
    messages = [
        {
            "role": "system",
            "content": """You're an AI assistant tasked with refining and enhancing error analysis reports for Yarado support staff. Your audience consists of technical experts in automation processes who need to thoroughly understand and address issues in client workflows. The output will be used to create a Slack message in a later step.

Your goals are to:
1. Remove any formatting currently present in the error context, cause analysis, and restart and solution sections.
2. Maintain the existing structure of the error description, cause analysis, and restart and solution sections, preserving all relevant information (except for introducing a new first 'summary' section).
3. Generate a new first section 'Brief summary of root cause, its technical impact, restart information, and key solution points' (using the "inverted pyramid" style for our narrative).
4. Enhance coherence between all sections, ensuring a logical flow of information.
5. Provide detailed explanations without being overly verbose. Aim for thoroughness rather than extreme conciseness.
6. Use a tone that is professional yet casual, friendly, and solution-oriented. Think of how you'd explain this to a knowledgeable colleague during a thorough discussion.
7. Use emojis very sparingly to add a touch of friendliness or to make the text a bit more appealing (1-5 max in the entire output, and only if it feels natural and adds value).
8. Remove any special formatting (i.e. markdown)
9. Ensure that statements about solutions or restart information are only included in their respective sections and the summary.
10. Carefully review and remove any premature statements about the cause of the error from the error description section.

NEVER apply any special formatting or structure to the text. Focus on refining the content and maintaining a tone that's both professional and approachable. Avoid adding unnecessary introductory or concluding sentences. NEVER USE MARKDOWN FORMATTING"""
        },
        {
            "role": "user",
            "content": f"""Hey there GPT! Please thoroughly analyse your system instructions and remember to follow them closely. Remember to act as a Yarado-employee and thus as a colleague of the one requesting this task.

            Here's the error context description (remove any statements regarding the cause of the error):

{error_description}

Here's the cause analysis (remove any statements regarding the resolution/solution/restart information for this error):

{cause_analysis}

And here's the restart and solution information:

{restart_and_solution}

The refined analysis should provide Yarado support staff with a clear, comprehensive understanding of:
1. Brief summary of root cause, its technical impact, restart information, and key solution points (3-5 sentences). Try to mention the step coordinates and step name (or range of coordinates and step names) so your colleagues can easily find the specific step (NOTE THIS IS A NEW SECTION YOU SHOULD GENERATE).
2. The task's technical overview
3. The error's location, context, and short historical overview (if present)
4. The observed behavior
5. The expected behavior
6. Historical and Similar Error Causes Comparison
7. The causal chain leading to the error (keep this a enumerated/bulleted list)
8. Detailed root cause analysis and its technical impact
9. Probability assessment (if applicable)
10. Restart Information
11. Solution Recommendations

If a probability assessment is present in the cause analysis, include it in the refined output; if not, omit this section without disrupting the flow of the analysis. 

This order should ensure the "inverted pyramid" style because the summary (including root cause, impact, restart info, and key solutions) is explicitly mentioned on top, whereas more detailed explanations are given thereafter.

Remember to use a professional yet casual, friendly, and solution-oriented tone because you're explaining this to a colleague during a detailed discussion - you are part of the support team of Yarado. Maintain professionalism and technical accuracy while being thorough in your explanations. NEVER add any formatting (so also NO MARKDOWN) or special characters for emphasis - focus solely on the content of the analysis (only some emojis are allowed 1-5 - and even desired). Start directly with the summary and end with the solution recommendations, without adding any introductory or concluding sentences."""
        }
    ]

    return retry_request_openai(client, messages)


async def format_for_slack(client, combined_analysis):
    messages = [
        {
            "role": "system",
            "content": "You are an AI assistant tasked with formatting a combined error analysis report into Slack JSON blocks. Your goal is to create a well-structured, easy-to-read message that adheres to Slack's formatting guidelines."
        },
        {
            "role": "user",
            "content": f"Here's the combined analysis:\n\n{combined_analysis}\n\nPlease format this analysis into Slack JSON blocks. Use appropriate formatting such as bold for headers, bullet points for lists, and code blocks for any code or variable names. Ensure the message is well-structured and easy to read in Slack. The output should be valid JSON that can be directly used in a Slack message."
        }
    ]

    return retry_request_openai(client, messages)


def find_json_by_key_value(json_list, key, value):
    for item in json_list:
        if isinstance(item, dict):  # Check if the item is a dictionary
            if item.get(key) == value:
                return item
        else:
            logging.warning(f"Non-dictionary item found in json_list: {item}")
    return None


async def staging_supporter(run_id, organisation_name, task_name):
    try:
        logging.info("Client initialized successfully.")

        log = load_log_file(run_id)
        image = load_screenshot(run_id)

        logging.info('Input data loaded successfully.')

        _, failed_step_id = determine_point_of_failure(log)
        logging.info('Point of failure determined.')

        preceding_steps_log = load_log_preceding_steps(log, failed_step_id, steps_to_include=10)
        logging.info('Preceding steps loaded.')

        # Fetch UARDI context
        uardi_context = await get_uardi_context(organisation_name=organisation_name, task_name=task_name,
                                                step_ids=[step['stepUuid'] for step in preceding_steps_log if
                                                          'stepUuid' in step], failed_step_id=failed_step_id)

        if uardi_context is None or uardi_context['main_task_data'] is None:
            logging.error("UARDI context is None. Exiting function.")
            return None

        logging.info('UARDI context loaded successfully.')
        print('UARDI context loaded successfully.')

        logging.info(f"preceding_steps_log type: {type(preceding_steps_log)}")
        logging.info(f"preceding_steps_log length: {len(preceding_steps_log)}")
        logging.info(f"failed_step_id: {failed_step_id}")

        if not preceding_steps_log:
            logging.warning(f"No preceding steps found for failed_step_id: {failed_step_id}")
            return None  # or handle this case appropriately

        failed_log_step_object = find_json_by_key_value(preceding_steps_log, 'stepUuid', failed_step_id)

        if failed_log_step_object is None:
            logging.warning(f"No step found with stepUuid: {failed_step_id}")
            return None  # or handle this case appropriately

        # Continue only if we have a valid failed_log_step_object
        failed_descr_step_object = uardi_context['step_descriptions'].get(failed_step_id, {})

        lookup_object = {
            "dev_cause": None,
            "dev_cause_enriched": None,
            "ai_context": None,
            "debug_pof": failed_log_step_object.get('debug', None),
            "type_pof": failed_descr_step_object.get('type', None),
            "name_pof": failed_log_step_object.get('name', None),
            "description_pof": failed_log_step_object.get('description', None),
            "ai_description_pof": failed_descr_step_object.get('original_ai_step_description', None),
            "payload_pof": failed_descr_step_object.get('original_step_payload', None)
        }

        print('Lookup object created successfully.')

        # Perform similarity search using the lookup object
        similar_errors_before_cause = await search_similar_errors(lookup_object, failed_step_id, absolute_threshold=0.6,
                                                                  relative_threshold=0.7)

        # Look up in Resolved Container if an error already occurred in this step -> Restart information can eventually only be retrieved from this source
        historical_resolved_errors = uardi_context.get('resolved_errors', [])

        # Create the combined overviews
        historical_error_overview, similar_error_overview = create_combined_error_overview(historical_resolved_errors,
                                                                                           similar_errors_before_cause)

        # Merge log data with task data
        merged_steps = merge_log_and_uardi(preceding_steps_log, uardi_context)

        logging.info('Log data and UARDI context merged successfully.')

        logging.info('Generation of error description started...')
        error_description = await generate_error_context(client=client, customer_name=organisation_name,
                                                         process_name=task_name,
                                                         steps_log=merged_steps,
                                                         screenshot=image, uardi_context=uardi_context,
                                                         historical_error_overview=historical_error_overview)

        logging.info('Error description generated successfully.')

        logging.info('Generation of cause analysis started...')
        cause_analysis = await perform_cause_analysis(client=client, customer_name=organisation_name,
                                                      process_name=task_name, steps_log=merged_steps, screenshot=image,
                                                      uardi_context=uardi_context,
                                                      ai_generated_error_context=error_description,
                                                      historical_error_overview=historical_error_overview,
                                                      similar_error_overview=similar_error_overview)
        logging.info('Cause analysis generated successfully.')

        # Create human like AI Cause
        human_like_ai_cause = await summarize_ai_cause(client=client, ai_cause=cause_analysis)

        lookup_object['dev_cause_enriched'] = human_like_ai_cause
        lookup_object['dev_cause'] = human_like_ai_cause

        similar_errors_after_cause = await search_similar_errors(lookup_object, failed_step_id, absolute_threshold=0.7,
                                                                 relative_threshold=0.7)

        # Finalize combined overview after cause analysis
        historical_error_overview, similar_error_overview = create_combined_error_overview(historical_resolved_errors,
                                                                                           similar_errors_after_cause)

        restart_and_solution = await generate_restart_information_and_solution(
            client=client,
            error_context=error_description,
            cause_analysis=cause_analysis,
            historical_error_overview=historical_error_overview,
            similar_error_overview=similar_error_overview
        )

        logging.info('Combining and refining analysis...')
        combined_analysis = await combine_and_refine_analysis(
            client, error_description, cause_analysis, restart_and_solution
        )
        logging.info('Analysis combined and refined successfully.')
        print(45*'-')
        print(combined_analysis)

        """
        # logging.info('Formatting analysis for Slack...')
        # G2. formatted_analysis = await format_for_slack(client, combined_analysis)
        # logging.info('Analysis formatted for Slack successfully.')
        #
        # return formatted_analysis"""

    except Exception as e:
        logging.error(f"An unexpected error occurred in staging_supporter: {e}")
        return None


if __name__ == "__main__":
    # Test data
    test_run_id = '3484c394-8de7-490b-bc9f-5de587a1ba82'
    test_organisation_name = 'Heinen & Hopman'
    test_task_name = 'orderbevestigingen_main'

    formatted_analysis = asyncio.run(staging_supporter(test_run_id, test_organisation_name, test_task_name))

    if formatted_analysis:
        print(f'\n\nFormatted analysis for Slack:\n\n{formatted_analysis}\n\n')
    else:
        print("Analysis generation or formatting failed.")
