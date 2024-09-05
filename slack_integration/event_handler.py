import logging
import json
import os
import asyncio
from datetime import datetime, timedelta
from openai import OpenAIError
from slack_sdk.errors import SlackApiError
from requests import RequestException
from slack_integration.message_handler import fetch_message, send_message, update_progress
from slack_integration.slack_client import get_bot_user_id
from azure.search.documents.aio import SearchClient
from azure.core.credentials import AzureKeyCredential
from utils.fetch_data import (
    load_screenshot, load_log_file, determine_point_of_failure,
    load_log_preceding_steps, extract_data_from_message, get_uardi_context,
    find_json_by_key_value, search_similar_errors, create_combined_error_overview,
    merge_log_and_uardi
)
from utils.constructor import (
    generate_error_context, perform_cause_analysis,
    generate_restart_information_and_solution, combine_and_refine_analysis,
    format_for_slack, summarize_ai_cause, assemble_blocks
)
from utils.post_process_and_update import (
    send_task_run_id_to_yarado, send_supporter_data_to_uardi
)
import html

# Uncomment below for local testing
#from dotenv import load_dotenv

# Load environment variables from .env file
#load_dotenv()

# Configure logging and set it to info
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Define the list of allowed channel IDs and valid reactions based on environment
CHANNEL_CONFIG = {
    'development': ['C07557UUU2K'],  # Test channel for development app
    'production': ['C07557UUU2K', 'C05D311FKPF', 'C05CFG7D0TU']  # Production channels
}

REACTION_CONFIG = {
    'development': ['test_tube'],  # Test reaction for development app
    'production': ['yara-sup-1', 'yara-sup-backup']  # Production reactions
}

# File-based storage for message states
MESSAGE_STATE_FILE = 'message_states.json'


def load_message_states():
    if os.path.exists(MESSAGE_STATE_FILE):
        with open(MESSAGE_STATE_FILE, 'r') as f:
            states = json.load(f)
            # Convert string timestamps back to datetime objects and ensure user_reactions is a set
            for key, state in states.items():
                state['last_processed'] = datetime.fromisoformat(state['last_processed'])
                state['user_reactions'] = set(state.get('user_reactions', []))
            return states
    return {}


def save_message_states(states):
    # Convert datetime objects to ISO format strings for JSON serialization
    # Convert user_reactions set to list for JSON serialization
    serializable_states = {
        key: {
            **state,
            'last_processed': state['last_processed'].isoformat(),
            'user_reactions': list(state['user_reactions'])
        } for key, state in states.items()
    }
    with open(MESSAGE_STATE_FILE, 'w') as f:
        json.dump(serializable_states, f)


def clean_old_message_states(states):
    three_days_ago = datetime.now() - timedelta(days=3)
    return {
        key: state for key, state in states.items()
        if state['last_processed'] > three_days_ago
    }


async def retry_block_assembly(openai_client, combined_analysis, slack_client, channel_id, progress_message_ts,
                               message_timestamp, max_retries=3):
    model = 'gpt-4o-2024-08-06'  # Start with gpt-4o-mini
    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"Attempt {attempt} to format blocks using model {model}")

            formatted_analysis = await format_for_slack(openai_client, combined_analysis, model=model)
            json_formatted_analysis = json.loads(formatted_analysis) if isinstance(formatted_analysis,
                                                                                   str) else formatted_analysis

            # Try assembling blocks
            slack_blocks_object, summary_content = assemble_blocks(json_formatted_analysis)

            # Success, return the assembled blocks and summary
            return slack_blocks_object, summary_content

        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logging.error(f"Block assembly failed on attempt {attempt}: {e}")
            # Update progress with the retry
            update_progress(slack_client, channel_id, progress_message_ts, 95, thread_ts=message_timestamp,
                            stage=f"retrying_block_formatting_{attempt}")

            # Retry with gpt-4o after the first attempt
            model = 'gpt-4o-2024-08-06'

        if attempt == max_retries:
            raise Exception("Max retries reached for block assembly.")


async def retry_sending_message(slack_client, channel_id, message_timestamp, openai_client, combined_analysis,
                                slack_blocks_object, summary_content, progress_message_ts, max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            # Attempt to send the message
            send_message(slack_client, channel_id, message_timestamp, slack_blocks_object['blocks'], as_text=False,
                         fallback_content=summary_content)
            logging.info(f"Slack message sent successfully on attempt {attempt}")
            return

        except SlackApiError as e:
            logging.error(f"Error sending Slack message on attempt {attempt}: {e}")

            # Retry block formatting if invalid blocks caused the failure
            if e.response['error'] == "invalid_blocks":
                logging.info(f"Retrying block assembly due to invalid blocks on attempt {attempt}")
                # Retry the block assembly
                slack_blocks_object, summary_content = await retry_block_assembly(openai_client, combined_analysis)

            # Update progress to inform about retries
            update_progress(slack_client, channel_id, progress_message_ts, 95, thread_ts=message_timestamp,
                            stage=f"retrying_message_sending_{attempt}")

        if attempt == max_retries:
            # If all retries fail, fallback to sending a simplified message
            logging.warning("All retries for sending message failed, falling back to summary message.")
            send_message(slack_client, channel_id, message_timestamp, summary_content, as_text=True)
            raise Exception("Max retries reached for Slack message sending.")


# Load existing message states
message_states = load_message_states()


async def send_error_message(slack_client, channel_id, message_timestamp, error_message):
    try:
        send_message(slack_client, channel_id, message_timestamp, error_message, as_text=True)
    except Exception as e:
        logging.error(f"Failed to send error message: {e}")


async def handle_event(data, environment, slack_client, openai_client):
    global message_states
    event = data.get('event', {})
    logging.info(f"Received event in {environment} environment: {event}")

    # Fetch bot's user ID
    bot_user_id = get_bot_user_id(slack_client)

    # Get the allowed channels and valid reactions for the given environment
    allowed_channels = CHANNEL_CONFIG.get(environment, [])
    valid_reactions = REACTION_CONFIG.get(environment, [])

    # Validate the Slack event
    try:
        validate_slack_event(event)
    except ValueError as ve:
        logging.error(f"Invalid Slack event: {ve}")
        return

    if event.get('type') in ['reaction_added', 'reaction_removed'] and event.get('reaction') in valid_reactions:
        user_id = event.get('user')
        reaction = event.get('reaction')

        # Skip events triggered by the bot itself
        if user_id == bot_user_id:
            logging.info("Skipping event triggered by the bot itself.")
            return

        channel_id = event['item']['channel']
        message_timestamp = event['item']['ts']

        # Ignore reactions in non-allowed channels
        if channel_id not in allowed_channels:
            logging.info("Reaction added in a non-allowed channel. Ignoring the event.")
            return

        # Get or create message state
        state_key = f"{channel_id}:{message_timestamp}"
        message_state = message_states.get(state_key, {
            'last_processed': datetime.min,
            'processing': False,
            'user_reactions': set()
        })

        if event['type'] == 'reaction_added':
            if user_id in message_state['user_reactions']:
                logging.info("This reaction has already been processed for this user.")
                return

            message_state['user_reactions'].add(user_id)

            # Check if we should process this message
            if (not message_state['processing'] and
                    (datetime.now() - message_state['last_processed']) > timedelta(minutes=5)):

                message_state['processing'] = True
                message_states[state_key] = message_state
                save_message_states(message_states)

                try:
                    await process_message(event, environment, slack_client, openai_client)
                finally:
                    message_state['processing'] = False
                    message_state['last_processed'] = datetime.now()
                    message_states[state_key] = message_state
                    save_message_states(message_states)

        elif event['type'] == 'reaction_removed':
            message_state['user_reactions'].discard(user_id)
            message_states[state_key] = message_state
            save_message_states(message_states)

    else:
        logging.info(f"Received unhandled event type: {event.get('type')}")

    # Clean old message states
    message_states = clean_old_message_states(message_states)
    save_message_states(message_states)


def validate_slack_event(event):
    required_fields = ['type', 'user', 'reaction', 'item']
    if not all(field in event for field in required_fields):
        raise ValueError("Invalid Slack event: missing required fields")

    if event['type'] not in ['reaction_added', 'reaction_removed']:
        raise ValueError(f"Unsupported event type: {event['type']}")

    if 'channel' not in event['item'] or 'ts' not in event['item']:
        raise ValueError("Invalid Slack event: missing item details")


async def process_message(event, environment, slack_client, openai_client):
    channel_id = event['item']['channel']
    message_timestamp = event['item']['ts']

    try:
        # Fetch the original message
        message = fetch_message(slack_client, channel_id, message_timestamp)
        if not message:
            raise ValueError("Failed to fetch the original message")

        logging.info("Fetched message successfully!")

        # Extract necessary data from the message
        client_name, task_name, prio, run_id = extract_data_from_message(message)
        client_name = html.unescape(client_name)
        task_name = html.unescape(task_name)

        logging.info(f"Client Name: {client_name}, Task Name: {task_name}, Prio: {prio}, Run ID: {run_id}")

        # Handle missing information
        if not all([client_name, task_name, run_id]):
            error_message = ":warning: Error: I was unable to extract the necessary information from the message :cry:.\n"
            if not client_name:
                error_message += "- Client Name could not be found.\n"
            if not task_name:
                error_message += "- Task Name could not be found.\n"
            if not run_id:
                error_message += "- Run ID could not be found."
            await send_error_message(slack_client, channel_id, message_timestamp, error_message)
            return

        # Handle specific task name cases
        if '.yrd' in task_name:
            error_message = (
                ":confused: Warning: It looks like this error originates from a user/manual triggered run. "
                "Currently, I am unable to handle these types of errors and am solely focused on cloud orchestrated runs. "
                "If you believe this is a mistake, please contact support (aka Torsten).")
            await send_error_message(slack_client, channel_id, message_timestamp, error_message)
            return

        # Send initial response to the user
        initial_message = ("Thanks for your request! I will take a moment to analyze the cause of this error. "
                           "Will come back to you ASAP :hourglass_flowing_sand:")
        initial_response = send_message(slack_client, channel_id, message_timestamp, initial_message, as_text=True)
        progress_message_ts = initial_response['ts']

        # Stage: Fetch Data
        update_progress(slack_client, channel_id, progress_message_ts, 10, thread_ts=message_timestamp,
                        stage="fetch_data")
        log_file, screenshot = await asyncio.gather(
            load_log_file(run_id),
            load_screenshot(run_id)
        )
        if log_file is None or log_file == "INVALID_JSON":
            raise ValueError("Unable to fetch the log file or invalid JSON format")
        if screenshot is None or screenshot == "INVALID_IMAGE":
            raise ValueError("Unable to fetch the screenshot or invalid image format")

        logging.info('Input data loaded successfully.')

        # Stage: Analyze Logs
        update_progress(slack_client, channel_id, progress_message_ts, 20, thread_ts=message_timestamp,
                        stage="analyze_logs")
        failed_step_id, catch_error_step_id, steps_between = determine_point_of_failure(log_file)
        if failed_step_id is None:
            raise ValueError("Could not determine the point of failure from the log file")

        preceding_steps_log = load_log_preceding_steps(
            log_file, failed_step_id,
            catch_error_step_id=catch_error_step_id,
            steps_to_include=10 + steps_between
        )
        if not preceding_steps_log:
            raise ValueError(f"No preceding steps found for failed_step_id: {failed_step_id}")

        logging.info('Log analysis completed.')

        # Stage: Context Generation
        update_progress(slack_client, channel_id, progress_message_ts, 30, thread_ts=message_timestamp,
                        stage="context_generation")
        async with SearchClient(
                endpoint=os.getenv("SEARCH_ENDPOINT"),
                index_name=os.getenv("SEARCH_INDEX_NAME"),
                credential=AzureKeyCredential(os.getenv("SEARCH_API_KEY"))
        ) as search_client:
            uardi_context = await get_uardi_context(
                organisation_name=client_name, task_name=task_name,
                step_ids=[step['stepUuid'] for step in preceding_steps_log if 'stepUuid' in step],
                failed_step_id=failed_step_id
            )
            if uardi_context is None or uardi_context['main_task_data'] is None:
                raise ValueError("UARDI context is None or invalid")

            catch_error = False
            if catch_error_step_id:
                failed_step_id = catch_error_step_id
                print('failed step id changed')
                catch_error = True

            failed_log_step_object = find_json_by_key_value(json.loads(log_file), 'stepUuid', failed_step_id)
            if failed_log_step_object is None:
                raise ValueError(f"No step found with stepUuid: {failed_step_id}")

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

            similar_errors_before_cause = await search_similar_errors(
                search_client=search_client,
                openai_client=openai_client,
                lookup_object=lookup_object,
                failed_step_id=failed_step_id,
                absolute_threshold=0.5,
                relative_threshold=0.7
            )

            historical_resolved_errors = uardi_context.get('resolved_errors', [])
            historical_error_overview, similar_error_overview = create_combined_error_overview(
                historical_resolved_errors,
                similar_errors_before_cause
            )

            merged_steps = merge_log_and_uardi(preceding_steps_log, uardi_context)

            logging.info('Context generation completed.')

            # Stage: Error Description
            update_progress(slack_client, channel_id, progress_message_ts, 50, thread_ts=message_timestamp,
                            stage="error_description")
            error_description = await generate_error_context(
                client=openai_client, customer_name=client_name,
                process_name=task_name, steps_log=merged_steps,
                screenshot=screenshot, uardi_context=uardi_context,
                historical_error_overview=historical_error_overview,
                catch_error_trigger=catch_error
            )
            logging.info('Error description generated successfully.')

            # Stage: Cause Analysis
            update_progress(slack_client, channel_id, progress_message_ts, 70, thread_ts=message_timestamp,
                            stage="cause_analysis")
            cause_analysis = await perform_cause_analysis(
                client=openai_client, customer_name=client_name,
                process_name=task_name, steps_log=merged_steps,
                screenshot=screenshot, uardi_context=uardi_context,
                ai_generated_error_context=error_description,
                historical_error_overview=historical_error_overview,
                similar_error_overview=similar_error_overview,
                catch_error_trigger=catch_error
            )

            human_like_ai_cause = await summarize_ai_cause(client=openai_client, ai_cause=cause_analysis)

            lookup_object['dev_cause_enriched'] = human_like_ai_cause
            lookup_object['dev_cause'] = human_like_ai_cause

            similar_errors_after_cause = await search_similar_errors(
                search_client=search_client,
                openai_client=openai_client,
                lookup_object=lookup_object,
                failed_step_id=failed_step_id,
                absolute_threshold=0.5,
                relative_threshold=0.7
            )

            historical_error_overview, similar_error_overview = create_combined_error_overview(
                historical_resolved_errors,
                similar_errors_after_cause
            )

            logging.info('Cause analysis completed.')

            # Stage: Solution Generation
            update_progress(slack_client, channel_id, progress_message_ts, 85, thread_ts=message_timestamp,
                            stage="solution_generation")
            restart_and_solution = await generate_restart_information_and_solution(
                client=openai_client,
                error_context=error_description,
                cause_analysis=cause_analysis,
                historical_error_overview=historical_error_overview,
                similar_error_overview=similar_error_overview
            )

            # Stage: Final Analysis
            update_progress(slack_client, channel_id, progress_message_ts, 95, thread_ts=message_timestamp,
                            stage="final_analysis")

            try:
                combined_analysis = await combine_and_refine_analysis(openai_client, error_description, cause_analysis,
                                                                      restart_and_solution)

                # Retry block assembly with proper retries
                slack_blocks_object, summary_content = await retry_block_assembly(openai_client, combined_analysis,
                                                                                  slack_client, channel_id,
                                                                                  progress_message_ts, message_timestamp)

                logging.info('Analysis formatted for Slack successfully.')

                # Retry sending the Slack message with up to 3 attempts
                await retry_sending_message(slack_client, channel_id, message_timestamp, openai_client,
                                            combined_analysis, slack_blocks_object, summary_content, progress_message_ts)

                # Remove the progress message after successful send
                slack_client.chat_delete(channel=channel_id, ts=progress_message_ts)

            except Exception as e:
                logging.error(f"Error during final analysis: {e}")

            # Prepare data for sending to UARDI and Yarado
            supporter_data = {
                "task_run_id": run_id,
                "task_name": task_name,
                "organisation_name": client_name,
                "step_id_pof": failed_step_id,
                "ai_cause": cause_analysis,
                "ai_description": error_description
            }

            yarado_data = {
                "task_run_id": run_id
            }

            if environment == 'production':  # Send data only in production mode
                try:
                    send_supporter_data_to_uardi(supporter_data)
                    send_task_run_id_to_yarado(yarado_data)
                except Exception as e:
                    logging.error(f"Failed to send data to UARDI or Yarado: {e}")
                    await send_error_message(slack_client, channel_id, message_timestamp,
                                             ":warning: Error: Failed to update external systems with the analysis results.")

    except ValueError as ve:
        logging.error(f"Value error occurred: {ve}")
        error_message = f":warning: An error occurred while processing your request: Invalid data format. Please check your input and try again."
        await send_error_message(slack_client, channel_id, message_timestamp, error_message)
    except RequestException as re:
        logging.error(f"Request error occurred: {re}")
        error_message = f":warning: An error occurred while communicating with external services. Please try again later."
        await send_error_message(slack_client, channel_id, message_timestamp, error_message)
    except OpenAIError as oe:
        logging.error(f"OpenAI API error: {oe}")
        error_message = f":warning: An error occurred while generating the analysis. Our AI service is currently experiencing issues. Please try again later."
        await send_error_message(slack_client, channel_id, message_timestamp, error_message)
    except SlackApiError as se:
        logging.error(f"Slack API error: {se}")
        error_message = f":warning: An error occurred while sending the message to Slack. Please try again or contact support."
        await send_error_message(slack_client, channel_id, message_timestamp, error_message)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        error_message = f":warning: An unexpected error occurred. Please try again later or contact support if the issue persists."
        await send_error_message(slack_client, channel_id, message_timestamp, error_message)