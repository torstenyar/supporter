from archive.src.azure_data_loader import load_task_data


def load_process_description():
    """
    Load the generic process description.
    """
    # Placeholder for loading process description logic
    process_description = {}
    return process_description


def load_recent_steps(client_name, main_task_name):
    """
    Load the recent steps before the error.
    """
    # Use the load_task_data function to get the task JSON data
    task_json, task_description = load_task_data(client_name, main_task_name)

    # Extract the recent steps from task_json
    #recent_steps = extract_recent_steps(task_json)  # Define this function based on your data structure
    #return recent_steps


def load_log_file(client_name, main_task_name):
    """
    Load the log file.
    """
    # Use the load_task_data function to get the task JSON data
    task_json, task_description = load_task_data(client_name, main_task_name)

    # Extract log file from task_json
    #log_file = extract_log_file(task_json)  # Define this function based on your data structure
    #return log_file


def load_screenshot(client_name, main_task_name):
    """
    Load the screenshot of the error moment.
    """
    # Placeholder for loading screenshot logic, if screenshots are also stored in Azure, you can extend the logic to download it
    screenshot = None
    return screenshot
