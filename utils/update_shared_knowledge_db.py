"""
We will create a script that handles incoming API requests
A json object with the following keys will be provided in the request:
1. organisation_name
2. task_name
3. organisation_id
4. task_run_id
5. datetime_of_error
6. time_spent
7. cause
8. solution

We will add some processing steps that will process the input request and transform it to enriched described causes
and analysis:
{
    "human_cause": "Robot couldn't find the 'next' button.", -> i.e. the 'cause' from the input request.
    "human_solution": "Replaced the screenshot steps with XPath steps.", -> i.e. the 'solution' from the input request.
    "ai_enriched_cause": "The 'next' button was likely dynamically loaded or hidden under certain conditions, making it difficult for the robot to locate using static screenshots. The log indicates that the button was not found during step 2.1.",
    "ai_enriched_solution": "Implement dynamic element detection techniques using XPath or CSS selectors that can adapt to changes in the DOM structure. Ensure all UI elements are consistently loaded before interaction. Regularly update the robot's detection logic to accommodate changes in the web page's layout or structure.",
    "ai_log_context": "The log file shows that the error occurred during step 2.1, right after the previous step was completed. This suggests that the issue is with the timing or loading of the 'next' button.",
    "ai_screenshot_analysis": "The screenshot reveals that the 'next' button was not visible at the time of the error. This could be due to the button being hidden or not loaded properly.",
    "ai_classified_common_error_type": "Typical RPA and automation errors"
}

After this step we have all the data we need to push it to our Azure DB.
"""