import json
from utils.ai_utils import retry_request_openai
import logging


async def generate_error_context(client, customer_name, process_name, steps_log, screenshot,
                                 uardi_context, historical_error_overview, catch_error_trigger=False):
    # Remove any sensitive information from uardi_context
    safe_uardi_context = uardi_context.copy()

    if 'main_task_data' in safe_uardi_context:
        safe_uardi_context['main_task_data'] = {
            k: v for k, v in safe_uardi_context['main_task_data'].items()
            if k not in ['id', 'organisation_id', 'overall', 'creation_date', 'last_updated', 'main_task_structure',
                         'step_descriptions', 'process_description', 'organisation_profile_last_updated', 'stats',
                         'last_request_date_time', '_rid', '_self', '_etag', '_attachments', '_ts']
        }

    catch_error_explanation = ""
    actual_error_steps_log = steps_log
    alternative_path_steps_log = []

    if catch_error_trigger:
        # Find the index of the 'CATCH ERROR STEP' event
        catch_error_index = next((i for i, step in enumerate(steps_log) if step.get('eventType') == 'CATCH ERROR STEP'),
                                 None)

        if catch_error_index is not None:
            # Split the steps log into actual error steps and alternative path steps
            actual_error_steps_log = steps_log[:catch_error_index + 1]
            alternative_path_steps_log = steps_log[catch_error_index:]

            catch_error_explanation = (
                "\n\nImportant Note: This error triggered a 'Catch Error' mechanism within the process. "
                "A 'Catch Error' is a predefined fallback path in the workflow, designed to handle specific failures. "
                "When a 'Catch Error' is triggered, the process diverts to an alternative sequence of steps aimed at mitigating the error or providing additional diagnostic information. Most of the times, this path leads to a forced error (often referred to as 'generate error'). This is deliberalty done by the human developers to notify the support team that something went wrong.\n\n"
                "The log has been split into two parts:\n"
                "1. **Actual Error Steps**: This contains the steps leading up to the 'Catch Error'. These steps are crucial for understanding the root cause of the error.\n"
                "2. **Alternative Path Steps**: This contains the steps that were executed after the 'Catch Error' was triggered. These steps are part of the fallback path and are less relevant to the root cause analysis.\n\n"
                "To determine the root cause, focus on the 'Actual Error Steps' section, as it contains the key events leading up to the error. The 'Alternative Path Steps' can provide additional context on how the process attempted to handle the error."
            )

    system_content = (
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
        "4. Screenshot: An image of the Azure VM screen at the moment the error occurred (always of size 1920x1080). This screenshot is a unique feature of the Yarado Client and provides crucial visual context. It can reveal:\n"
        "   - The state of the application or website being interacted with\n"
        "   - Any visible error messages or unexpected UI states\n"
        "   - The presence of pop-ups or system notifications\n"
        "   - The overall desktop environment and any relevant background processes\n"
        "   - Timestamps or other temporal information visible on the screen\n"
        "   The screenshot should be analyzed in conjunction with the log data to provide a more comprehensive understanding of the error context. It may reveal issues not apparent in the logs alone, such as network disconnections, unexpected application behavior, or system-level issues. Also, very important, is the location in the metadata/debug data. If you know the normal format of the screen is 1920x1080 you might discover where the robot wanted to click by looking at the coords (altough note it might be relative coordinates, not always absolute)\n\n"
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
                "- Do not explain the benefits of automation or why the process was automated.\n"
                "- Never explain the benefits of automation or why the process was automated.\n"
                "- Avoid business jargon; stick to technical terminology.\n"
                "- Do not speculate on causes or offer analysis.\n"
                "- Use plain text formatting without special structuring."
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

    user_content = (
        "Hi GPT, thoroughly analyse your system instructions and remember to follow them closely. "
        "Remember to act as a Yarado-employee and thus as a colleague of the one requesting this task.\n\n"
        "Generate a technical context overview for the error based on these inputs:\n\n"
        "1. Historical error information:\n>>>\n{historical_error_overview}\n>>>\n"
        "2. Task and organization information:\n>>>\n{uardi_context}\n>>>\n"
        "3. Log data of the last {steps} steps:\n\n"
        "Actual Error Steps:\n>>>\n{actual_error_steps_log}\n>>>\n"
        "Alternative Path Steps (following 'Catch Error' if present, ignore if it is empty - i.e. no steps are shown):\n>>>\n{alternative_path_steps_log}\n>>>\n"
        "4. Screenshot of the window just before the error (attached).\n\n"
        "{catch_error_instruction}\n\n"
        "Provide a comprehensive technical context that will help Yarado support staff quickly understand the task's technical flow, "
        "where in the process the error occurred, and what was being attempted from a systems and data perspective. "
        "Focus on technical details that are directly relevant to troubleshooting the error. "
        "Make sure to incorporate insights from the screenshot throughout your analysis, particularly in describing the observed behavior."
        "Remember that this section purely focuses on giving context about the error - NEVER indicate a potential cause or solution in this section.\n\n"
        "{catch_error_explanation}"
    ).format(
        steps=len(steps_log) - 1,
        actual_error_steps_log=json.dumps(actual_error_steps_log, indent=2),
        alternative_path_steps_log=json.dumps(alternative_path_steps_log, indent=2),
        uardi_context=json.dumps(safe_uardi_context['main_task_data'], indent=2),
        historical_error_overview=historical_error_overview,
        catch_error_instruction="IMPORTANT: This error scenario involves a catch error mechanism. In your response, prioritize explaining the catch error, its trigger point, and its implications in the 'Error Location, Context, and Historical Overview' section." if catch_error_trigger else "",
        catch_error_explanation=catch_error_explanation
    )

    messages = [
        {
            "role": "system",
            "content": system_content
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_content},
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
                                 uardi_context, ai_generated_error_context, historical_error_overview,
                                 similar_error_overview, catch_error_trigger=False):
    # Remove any sensitive information from uardi_context
    safe_uardi_context = uardi_context.copy()
    if 'main_task_data' in safe_uardi_context:
        safe_uardi_context['main_task_data'] = {
            k: v for k, v in safe_uardi_context['main_task_data'].items()
            if k not in ['id', 'organisation_id', 'overall', 'tasks', 'creation_date', 'last_updated',
                         'main_task_structure', 'process_description', 'organisation_profile_last_updated', 'stats',
                         'last_request_date_time', '_rid', '_self', '_etag', '_attachments', '_ts']
        }

    catch_error_explanation = ""
    actual_error_steps_log = steps_log
    alternative_path_steps_log = []
    causal_chain_instruction = ""

    if catch_error_trigger:
        # Find the index of the 'CATCH ERROR TRIGGER' event
        catch_error_index = next(
            (i for i, step in enumerate(steps_log) if
             step.get('eventType') == 'FAILED STEP THAT CAUSED THE CATCH ERROR TRIGGER'), None)

        if catch_error_index is not None:
            # Get the 10 steps prior to the catch error trigger, or all steps if less than 10
            start_index = max(0, catch_error_index - 10)
            actual_error_steps_log = steps_log[start_index:catch_error_index + 1]
            alternative_path_steps_log = steps_log[catch_error_index:]

            catch_error_explanation = (
                "\n\nImportant Note: The error encountered in this process triggered a 'Catch Error' mechanism. "
                "When a 'Catch Error' is triggered, the process diverts to an alternative sequence of steps aimed at mitigating the error or providing additional diagnostic information. Most of the times, this path leads to a forced error (often referred to as 'generate error'). This is deliberalty done by the human developers to notify the support team that something went wrong."
                "This indicates that the process diverted to an alternative sequence of steps due to a predefined failure handling path.\n\n"
                "The log has been split into two parts:\n"
                "1. **Actual Error Steps**: These are the steps leading up to the 'Catch Error'. This section contains the crucial events that likely caused the error.\n"
                "2. **Alternative Path Steps**: These are the steps executed after the 'Catch Error' was triggered. This sequence represents the fallback path taken by the process.\n\n"
                "For a thorough cause analysis, focus on the 'Actual Error Steps' to identify the root cause of the error, while the 'Alternative Path Steps' provide context on how the process attempted to manage the error."
            )

            causal_chain_instruction = (
                "\n\nWhen analyzing the causal chain, pay special attention to the steps leading up to the 'FAILED STEP THAT CAUSED THE CATCH ERROR TRIGGER' event. "
                "The provided 'Actual Error Steps' log contains up to 10 steps prior to this event. These steps are crucial for understanding "
                "the sequence of events that led to the catch error being triggered. Ensure your analysis thoroughly examines these preceding steps, "
                "as they likely contain the root cause of the issue that necessitated the catch error mechanism."
            )

    system_content = (
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
        "6. Screenshot: An image of the Azure VM screen at the moment the error occurred (always of size 1920x1080). This screenshot is a unique feature of the Yarado Client and provides crucial visual context. It can reveal:\n"
        "   - The state of the application or website being interacted with\n"
        "   - Any visible error messages or unexpected UI states\n"
        "   - The presence of pop-ups or system notifications\n"
        "   - The overall desktop environment and any relevant background processes\n"
        "   - Timestamps or other temporal information visible on the screen\n"
        "   The screenshot should be analyzed in conjunction with the log data to provide a more comprehensive understanding of the error context. It may reveal issues not apparent in the logs alone, such as network disconnections, unexpected application behavior, or system-level issues. Also, very important, is the location in the metadata/debug data. If you know the normal format of the screen is 1920x1080 you might discover where the robot wanted to click by looking at the coords (altough note it might be relative coordinates, not always absolute)\n\n"
        "OUTPUT:"
        "Structure your response as follows:\n"
        "5. Historical and Similar Error Causes Comparison:\n"
        "   - Briefly compare the current error with historical errors causes at this step. You are encouraged to repeat/quote earlier causes written by developers.\n"
        "   - Highlight any recurring patterns or notable differences in historical errors causes.\n"
        "   - Discuss how similar errors (from the RAG model) relate to the current error, noting that they may not be from the exact same step.\n"
        "   - Mention developers who have frequently addressed similar or historical issues, if this information is available. Only tell this if it is a obvious one, and the historical error solver weigh much heavier than a similar error solver.\n"
        "   - Compare the visual state in the current screenshot with any descriptions of visual states in historical or similar errors.\n\n"
        "6. Causal Chain Analysis:\n"
        "   - Provide a concise step-by-step breakdown of events leading to the error. If a catch error flow was followed, the causal chain should lead up to this step (with eventType == 'FAILED STEP THAT CAUSED THE CATCH ERROR TRIGGER')\n"
        "   - For each relevant step, describe its action, impact, and any variable changes. Use the 'original_ai_step_description' for context.\n"
        "   - Use the format: 'Step X.Y: [Concise description of action, impact, and key variables]'\n"
        "   - Focus on variable values, their logic in the process context, and potential contribution to the error.\n"
        "   - Draw connections between steps to illustrate the causal progression.\n"
        "   - Pay special attention to steps preceding the error. Analyze whether these steps completed successfully and as expected.\n"
        "   - Consider environmental factors that might affect step execution, such as page loading issues or data availability.\n"
        "   - If relevant, compare the current causal chain with patterns observed in historical errors at similar steps.\n"
        "   - Explicitly state your reasoning for inferring the success or failure of each step, as there are no explicit status indicators in the log data.\n"
        "   - Relate your observations from the log data to what you see in the screenshot, explaining any correlations or discrepancies.\n"
        f"{causal_chain_instruction}\n\n"
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
        f"{catch_error_explanation}"
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

    user_content = (
        "Hi GPT, thoroughly analyse your system instructions and remember to follow them closely. Remember to act as a Yarado-employee and thus as a colleague of the one requesting this task.\n\n"
        "Perform a detailed cause analysis based on the following inputs:\n\n"
        "1. Historical error information:\n>>>\n{historical_error_overview}\n>>>\n"
        "2. Similar error information:\n>>>\n{similar_error_overview}\n>>>\n"
        "3. Previously generated error context:\n>>>\n{ai_generated_error_context}\n>>>\n"
        "4. Log data of the last {steps} steps:\n\n"
        "Actual Error Steps:\n>>>\n{actual_error_steps_log}\n>>>\n"
        "Alternative Path Steps (following 'Catch Error' if present, ignore if it is empty - i.e. no steps are shown):\n>>>\n{alternative_path_steps_log}\n>>>\n"
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
        f"{catch_error_explanation}"
        f"{causal_chain_instruction}"
    ).format(
        ai_generated_error_context=ai_generated_error_context,
        uardi_context=json.dumps(safe_uardi_context['main_task_data'], indent=2),
        steps=len(steps_log) - 1,
        actual_error_steps_log=json.dumps(actual_error_steps_log, indent=2),
        alternative_path_steps_log=json.dumps(alternative_path_steps_log, indent=2),
        historical_error_overview=historical_error_overview,
        similar_error_overview=similar_error_overview
    )

    messages = [
        {
            "role": "system",
            "content": system_content
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_content},
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

    summary = retry_request_openai(client, messages, model='gpt-4o')
    return summary


async def generate_restart_information_and_solution(client, error_context, cause_analysis, historical_error_overview,
                                                    similar_error_overview):
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
1. Summary of root cause, its technical impact, restart information, and key solution points (3-5 sentences). Mention the step coordinates and step name (or range of coordinates and step names) so your colleagues can easily find the specific step. Also explicitly mention the step coords for the restart location and in which loop the task should be restarted, but only state this if you know this. Restart location you should discover by analysing historical data, but the loop in which you should restart, if we were in a loop, should be deterimined based on the loop in which we were - so not from the historical data as this data could be in other loop row numbers. BE AS EXPLICIT AS POSSIBLE IN THIS SECTION! (NOTE THIS IS A NEW SECTION YOU SHOULD GENERATE).
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


async def format_for_slack(client, combined_analysis, model='gpt-4o'):
    messages = [
        {
            "role": "system",
            "content": """You are an AI assistant tasked with formatting a combined error analysis report into Slack JSON blocks. The desired JSON scheme is provided to you. Output explicitly this scheme and this scheme only. Your goal is to create a well-structured, easy-to-read message that adheres to Slack's formatting guidelines. Ensure each section is appropriately formatted with headers, bullet points, and text blocks where necessary. For step coordinates, and step names, etc., use ` to display it as code style text. NEVER REMOVE ANY TEXT OR CONTENT. YOUR ROLE IS TO TRANSFORM THE STRUCTURE NOT TO CHANGE THE CONTENT!!
            In the end, we are creating an analysis containing this information:
            1. Summary of root cause, its technical impact, restart information, and key solution points.
            2. The task's technical overview
            3. The error's location, context, and short historical overview (if present)
            4. The observed behavior
            5. The expected behavior
            6. Historical and Similar Error Causes Comparison
            7. The causal chain leading to the error (keep this a enumerated/bulleted list)
            8. Detailed root cause analysis and its technical impact
            9. Probability assessment (if applicable, is not always present).
            10. Restart Information
            11. Solution Recommendations

The user will provide you with a JSON scheme that you strictly follow and fill in. Within this JSON scheme the user will give some dummy examples on how the text could be formatted. Use this as inspiration when transforming the current text into this schema. I want to remind you to NEVER use double stars (**) in the formatting and always start a section with a bolded (single star) sentence representing the header - as shown in the examples. You are free to make up the words in this representative header of the section. ONLY FILL IN THE BLOCK IF THE CORRESPONDING CONTENT IS IN THE INPUT DATA, OTHERWISE LEAVE A EMPTY STRING.
            """
        },
        {
            "role": "user",
            "content": f"Here's the combined analysis:\n\n{combined_analysis}\n\nPlease format this analysis into Slack JSON blocks. Determine for yourself which content needs to be placed in which JSON block. Use appropriate formatting such as bold for headers, bullet points for lists, and code blocks for any code or variable names. Ensure the message is well-structured and easy to read in Slack. The output should be valid JSON that can be directly used in a Slack message. The formatting should also include subtle use of emojis where appropriate. However, this change of formatting and styling is the only thing you may do - never change the actual content of the text."
        }
    ]

    slack_json_schema = {
        "name": "slack_message_schema",
        "description": "Schema for formatting a Slack message containing a structured error analysis report.",
        "strict": True,
        "schema": {
            "type": "object",
            "description": "A structured Slack message divided into different blocks.",
            "properties": {
                "block1": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "🚨 *Summary of root cause, its technical impact, restart information, and key solution points.*\n*At step `12,3` named \"Perform Action\" in the `example_task` task*, an error occurred due to a missing configuration file, which halted the workflow. Restarting the task from this step after resolving the configuration issue should restore functionality."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block3": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "🔍 *Technical overview.*\nThe `example_task` task automates various actions within the system, involving 50 steps including data validation, file processing, and API calls."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block5": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "🗺 *The error's location, context, and short historical overview (if present).* The error occurred during the \"Process Data\" subtask at step `14,5`, which failed due to a timeout error. This step is critical for data processing, and historically, similar errors have occurred due to network instability."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block7": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "👀 *The observed behavior.*\nDuring the execution of step `14,5`, the system encountered a timeout error, resulting in incomplete data processing."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block9": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "🎯 *The expected behavior.*\nThe task should have successfully processed the data without encountering any timeout errors, completing all steps as expected."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block11": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "📊 *Historical and Similar Error Causes Comparison.*\nHistorically, similar timeout errors have occurred due to network issues during data processing steps. Previous incidents were resolved by improving network stability and adjusting timeout settings."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block13": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "🔗 *The causal chain leading to the error.*\n- `Step 12.2`: Data validation completed successfully.\n- `Step 14.1`: API call initiated, no issues detected.\n- `Step 14.5`: Timeout error occurred during data processing."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block15": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "🔍 *Detailed root cause analysis and its technical impact.*\nThe root cause of the error is identified as a timeout during data processing, which is a recurrent issue due to network instability. This impacts the reliability of the task and could lead to data inconsistencies."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block17": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "🔮 *Probability assessment.*\nBased on the available data, the likelihood of a network issue causing the timeout is approximately 80%, while the possibility of a coding error is around 20%."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block19": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "🔄 *Restart information.*\nTo resolve the issue, restart the task from step `14,5` after ensuring that network stability is improved."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                },
                "block21": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["section"],
                            "description": "Type of block element."
                        },
                        "text": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["mrkdwn"],
                                    "description": "The type of text formatting."
                                },
                                "text": {
                                    "type": "string",
                                    "description": "💡 *Solution recommendations.*\n1. *Network Stability:* Improve the network infrastructure to prevent similar timeout issues.\n2. *Error Handling:* Implement better error handling for timeout scenarios to allow for automatic retries.\n3. *Logging Enhancements:* Increase the detail in logs to provide better insights during troubleshooting."
                                }
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        }
                    },
                    "required": ["type", "text"],
                    "additionalProperties": False
                }
            },
            "required": [
                "block1",
                "block3",
                "block5",
                "block7",
                "block9",
                "block11",
                "block13",
                "block15",
                "block17",
                "block19",
                "block21"
            ],
            "additionalProperties": False
        }
    }

    return retry_request_openai(
        client=client,
        messages=messages,
        model=model,
        json_schema=slack_json_schema
    )


def assemble_blocks(ai_output):
    """Convert the AI output into a Slack message format and return the summary block separately."""
    slack_message = {"blocks": []}
    summary_text = ""  # To store the content of the summary block
    valid_blocks = []  # To collect blocks that are not empty

    # Iterate through the blocks and build the message
    try:
        for key in sorted(ai_output.keys(), key=lambda x: int(x.replace('block', ''))):
            block = ai_output[key]

            # Ensure the block text is not empty
            if block.get('type') == 'section' and 'text' in block and 'text' in block['text']:
                block_text = block['text']['text'].strip()  # Strip any surrounding whitespace

                # Check if the block text is valid
                if block_text:
                    # Add the section block to the message
                    valid_blocks.append({
                        "type": block['type'],
                        "text": {
                            "type": block['text']['type'],
                            "text": block_text
                        }
                    })

                    # Capture the summary block's content
                    if key == "block1":  # Assuming block1 is the summary block
                        summary_text = block_text

        # Insert dividers between blocks (but not after the last one)
        for i, block in enumerate(valid_blocks):
            slack_message['blocks'].append(block)
            if i < len(valid_blocks) - 1:  # Avoid adding a divider after the last block
                slack_message['blocks'].append({"type": "divider"})

        return slack_message, summary_text

    except KeyError as e:
        logging.error(f"Missing key in AI output: {e}")
        raise
    except TypeError as e:
        logging.error(f"Type error in block assembly: {e}")
        raise
    except ValueError as e:
        logging.error(f"Value error during block assembly: {e}")
        raise


