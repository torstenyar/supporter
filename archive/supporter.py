
def supporter():
    # Triggered by a user placing a reaction with :yar-sup: on a slack message in the #notifications-error channel
    # Fetch message to which the reaction was placed

    # Load log data
    log_file = load_log_file() # Load the log file of the yarado process where the error occured (process name and run ID can be found in the error message from slack, log file can be extracted with a GET request)
    screenshot = load_screenshot() # Load the screenshot of the yarado process where the error occured (process name and run ID can be found in the error message from slack, screenshot can be extracted with a GET request)

    # Determine point of failure based on log file (ID of step which first failed and where no succesful steps were completed thereafter).

    # Check if the process data and described steps are available in the azure table
    # If available, load process descriptions and preceding step descriptions directly from azure:
    process_description = load_process_description()
    preceding_steps = load_preceding_steps() # USE ID of PoF (not step coords or names)
    # Compare with steps in log file, if a step id is present in the last 10 steps taken in the log file but not present in the described steps, let the process describe these steps first.

    # Else (if row is not present in azure table):
    # Generate step descriptions and create process description and push to azure, then load the process description and preceding step descriptions directly from azure
    process_description = load_process_description()
    preceding_steps = load_preceding_steps() # USE ID of PoF (not step coords or names)

    # Generate error description (objective description of the error).
    error_description = generate_error_description(recent_steps, log_file, process_description, screenshot)

    # Perform cause analysis (try to determine the cause of the error by reviewing the input data thoroughly). Important to let the model argument why this is most probably the cause
    cause_analysis = perform_cause_analysis(error_description, recent_steps, log_file, process_description, screenshot)

    # Suggest resolution (try to come up with a solution and try to make it software specific)
    resolution = suggest_resolution(error_description, cause_analysis)

    # React to the slack message which triggered the process in the first place. Write the error description, cause analysis (including chain of thought), and resolution


if __name__ == "__main__":
    supporter()
