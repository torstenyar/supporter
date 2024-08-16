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
        return "No failed step found with the provided step ID"

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


async def generate_error_context(client, customer_name, process_name, point_of_failure, steps_log, screenshot, uardi_context):
    # Remove any sensitive information from uardi_context
    safe_uardi_context = uardi_context.copy()
    if 'main_task_data' in safe_uardi_context:
        safe_uardi_context['main_task_data'] = {
            k: v for k, v in safe_uardi_context['main_task_data'].items()
            if k not in ['id', 'organisation_id', 'overall', 'creation_date', 'last_updated', 'main_task_structure', 'process_description', 'organisation_profile_last_updated', 'stats', 'last_request_date_time', '_rid', '_self', '_etag', '_attachments', '_ts']
        }
    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI assistant designed to help Yarado support staff understand the technical context of errors in automated workflows. "
                "Your audience consists of highly technical Yarado employees who are familiar with automation processes and systems.\n\n"
                "Context:\n"
                "The process '{process_name}' was developed for the customer '{customer_name}'. Your task is to provide a clear, concise, and technically focused description of the error context.\n\n"
                "UARDI Data Structure:\n"
                "- The 'organisation_profile' field contains information about the client's business domain.\n"
                "- The 'ai_task_summary' field provides an overview of the task's purpose and workflow.\n"
                "- The 'tasks' key provides details about the main task and its subtasks, including loop information.\n\n"
                "Log Data Structure:\n"
                "The log of steps contains merged data from both the runtime log and the original task file:\n"
                "- 'original_ai_step_description': This field comes from the original task file, not the runtime log. "
                "  It contains an AI-generated description of what the step is supposed to do.\n"
                "- 'original_step_payload': This field also comes from the original task file. It contains the original "
                "  configuration or parameters for the step as defined in the task.\n"
                "These fields provide context about the intended behavior of each step, which can be crucial when "
                "comparing against what actually happened during execution.\n\n"
                "Structure your response as follows:\n"
                "1. Task Technical Overview: Briefly describe the high-level technical flow of the main task (derive this from the summary, and only the main object in the task JSON object - not the nested subtasks). Focus on:\n"
                "   - Systems and websites involved\n"
                "   - Types of data processed\n"
                "   - Key data processing steps\n"
                "   - RPA, AI, APIs or integration points (if present)\n"
                "   Present this information densely, assuming high technical knowledge of the audience. Never mention the number of steps in the task in this section.\n"
                "2. Error Location and Context: Specify the exact step coordinate - how this relate to maintask/subtask and loop. In the point of failure descritpion you will see the task in which the step failed - whether it is a subtask step or a maintask step, relate this to the corresponding object in the 'tasks' object, in which loop the process was (if we were in a loop), and task where the error occurred, and indicate how far the process probably was. Include step coordinates and indicate the error's position relative to the overall process flow. This should follow logically after previous part on task technical overview, indicate how it relates to this part and where in the flow this error occured.\n"
                "3. Observed Behavior: Describe the observable technical facts from the log and screenshot.\n"
                "4. Expected Behavior: Briefly mention the expected technical outcome at this point in the process.\n\n"
                "Important:\n"
                "- Focus solely on technical aspects relevant to troubleshooting.\n"
                "- Do not explain the benefits of automation or why the process was automated.\n"
                "- Avoid business jargon; stick to technical terminology.\n"
                "- Do not speculate on causes or offer analysis.\n"
                "- Use plain text formatting without special structuring."
            ).format(
                customer_name=customer_name,
                process_name=process_name
            )
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Generate a technical context overview for the error based on these inputs:\n\n"
                    "1. Point of failure description:\n>>>\n{point_of_failure}\n>>>\n"
                    "2. Log data of the last {steps} steps:\n>>>\n{steps_log}\n>>>\n"
                    "3. Task and organization information:\n>>>\n{uardi_context}\n>>>\n"
                    "4. Screenshot of the window just before the error (attached).\n\n"
                    "Provide a comprehensive technical context that will help Yarado support staff quickly understand the task's technical flow, "
                    "where in the process the error occurred, and what was being attempted from a systems and data perspective. "
                    "Focus on technical details that are directly relevant to troubleshooting the error."
                ).format(
                    point_of_failure=point_of_failure,
                    steps=len(steps_log) - 1,
                    steps_log=json.dumps(steps_log, indent=2),
                    uardi_context=json.dumps(safe_uardi_context['main_task_data'], indent=2)
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


async def perform_cause_analysis(client, customer_name, process_name, point_of_failure, steps_log, screenshot,
                                 uardi_context, ai_generated_error_context):
    # Remove any sensitive information from uardi_context
    safe_uardi_context = uardi_context.copy()
    if 'main_task_data' in safe_uardi_context:
        safe_uardi_context['main_task_data'] = {
            k: v for k, v in safe_uardi_context['main_task_data'].items()
            if k not in ['id', 'organisation_id', 'overall', 'tasks', 'creation_date', 'last_updated', 'main_task_structure', 'process_description', 'organisation_profile_last_updated', 'stats', 'last_request_date_time', '_rid', '_self', '_etag', '_attachments', '_ts']
        }

    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI assistant specialized in analyzing errors in Yarado's automated workflows. "
                "Your audience consists of highly technical Yarado employees who are experts in automation processes and systems.\n\n"
                "Context:\n"
                "The process '{process_name}' for customer '{customer_name}' has encountered an error. "
                "An error description and context have already been provided. Your task is to perform a detailed cause analysis.\n\n"
                "UARDI Data Structure:\n"
                "- The 'organisation_profile' field contains information about the client's business domain.\n"
                "- The 'ai_task_summary' field provides an overview of the task's purpose and workflow.\n"
                "- The 'step_descriptions' contain AI-generated descriptions and payloads for relevant steps.\n\n"
                "Log Data Structure:\n"
                "The log of steps contains merged data from both the runtime log and the original task file:\n"
                "- 'original_ai_step_description': This field comes from the original task file, not the runtime log. "
                "  It contains an AI-generated description of what the step is supposed to do.\n"
                "- 'original_step_payload': This field also comes from the original task file. It contains the original "
                "  configuration or parameters for the step as defined in the task.\n"
                "These fields provide context about the intended behavior of each step, which can be crucial when "
                "comparing against what actually happened during execution.\n\n"
                "Structure your response as follows:\n"
                "1. Root Cause Identification:\n"
                "   - Determine the fundamental reason for the error, looking beyond immediate triggers.\n"
                "   - Explain your reasoning, citing specific evidence from logs, screenshot, or UARDI data.\n"
                "2. Causal Chain Analysis:\n"
                "   - Provide a step-by-step breakdown of events leading to the error.\n"
                "   - For each relevant step, describe its action, impact, and any variable changes.\n"
                "   - Use the format: 'Step X.Y: [Description of action, impact, and variables]'\n"
                "   - Analyze variable values, their logic in the process context, and potential contribution to the error.\n"
                "   - Draw connections between steps to illustrate the causal progression.\n"
                "3. Technical Impact Assessment:\n"
                "   - Explain how the root cause affects the overall process from a technical perspective.\n"
                "   - Discuss any potential ripple effects on other systems or processes.\n"
                "4. Probability Analysis (if applicable):\n"
                "   - If multiple causes are possible, rank them by likelihood and explain your reasoning.\n"
                "   - Consider how variable values and changes factor into this assessment.\n\n"
                "Important:\n"
                "- Focus on technical aspects relevant to troubleshooting and resolution.\n"
                "- Use technical terminology appropriate for expert Yarado staff.\n"
                "- Ensure your analysis logically follows and builds upon the provided error context.\n"
                "- Do not repeat information from the error context unless directly relevant to cause analysis.\n"
                "- Provide actionable insights for addressing the root cause.\n"
                "- Use plain text formatting without special structuring."
            ).format(
                customer_name=customer_name,
                process_name=process_name
            )
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Perform a detailed cause analysis based on the following inputs:\n\n"
                    "1. Previously generated error context:\n>>>\n{ai_generated_error_context}\n>>>\n"
                    "2. Point of failure description:\n>>>\n{point_of_failure}\n>>>\n"
                    "3. Log data of the last {steps} steps:\n>>>\n{steps_log}\n>>>\n"
                    "4. Task and organization information:\n>>>\n{uardi_context}\n>>>\n"
                    "5. Screenshot of the window just before the error (attached).\n\n"
                    "Provide a comprehensive cause analysis that logically follows and builds upon the error context. "
                    "Focus on identifying the root cause, detailing the causal chain of events, and offering actionable insights "
                    "for Yarado support staff to address the issue effectively."
                ).format(
                    ai_generated_error_context=ai_generated_error_context,
                    point_of_failure=point_of_failure,
                    steps=len(steps_log) - 1,
                    steps_log=json.dumps(steps_log, indent=2),
                    uardi_context=json.dumps(safe_uardi_context, indent=2)
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


async def combine_and_refine_analysis(client, error_description, cause_analysis):
    messages = [
        {
            "role": "system",
            "content": "You are an AI assistant tasked with combining and refining error analysis reports for Yarado support staff. Your goal is to create a cohesive, non-repetitive narrative that clearly explains what happened and why, based on the provided error description and cause analysis."
        },
        {
            "role": "user",
            "content": f"Here's the error description:\n\n{error_description}\n\nAnd here's the cause analysis:\n\n{cause_analysis}\n\nPlease combine these into a single, fluid narrative. Remove any redundant information and ensure the flow is logical and easy to follow. The final output should provide a clear understanding of the error, its context, and its root cause(s)."
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


async def staging_supporter(run_id, organisation_name, task_name):
    try:
        logging.info("Client initialized successfully.")

        log = load_log_file(run_id)
        image = load_screenshot(run_id)
        logging.info('Input data loaded successfully.')

        point_of_failure_descr, failed_step_id = determine_point_of_failure(log)
        logging.info('Point of failure determined.')

        preceding_steps_log = load_log_preceding_steps(log, failed_step_id, steps_to_include=10)
        logging.info('Preceding steps loaded.')

        uardi_context = await get_uardi_context(organisation_name, task_name,
                                                [step['stepUuid'] for step in preceding_steps_log if
                                                 'stepUuid' in step])
        logging.info('UARDI context loaded successfully.')

        # Merge log data with task data
        merged_steps = merge_log_and_uardi(preceding_steps_log, uardi_context)
        logging.info('Log data and UARDI context merged successfully.')

        logging.info('Generation of error description started...')
        error_description = await generate_error_context(client, organisation_name, task_name,
                                                             point_of_failure_descr, merged_steps, image, uardi_context)

        logging.info('Error description generated successfully.')
        print(error_description)
        """
        logging.info('Generation of cause analysis started...')
        cause_analysis = await perform_cause_analysis(
            client, organisation_name, task_name, merged_steps, image,
            error_description, uardi_context
        )
        logging.info('Cause analysis generated successfully.')

        logging.info('Combining and refining analysis...')
        combined_analysis = await combine_and_refine_analysis(
            client, error_description, cause_analysis
        )
        logging.info('Analysis combined and refined successfully.')

        logging.info('Formatting analysis for Slack...')
        formatted_analysis = await format_for_slack(client, combined_analysis)
        logging.info('Analysis formatted for Slack successfully.')

        return formatted_analysis"""

    except Exception as e:
        logging.error(f"An unexpected error occurred in staging_supporter: {e}")
        return None


if __name__ == "__main__":
    # Test data
    test_run_id = '112295c2-cf02-4e2c-93ec-e369d1e6a214'
    test_organisation_name = 'Ultimoo Incasso B.V.'
    test_task_name = 'Email-Classification-and-Summary-main_Incasso_inbox'

    formatted_analysis = asyncio.run(staging_supporter(test_run_id, test_organisation_name, test_task_name))

    if formatted_analysis:
        print(f'\n\nFormatted analysis for Slack:\n\n{formatted_analysis}\n\n')
    else:
        print("Analysis generation or formatting failed.")
