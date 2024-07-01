from azure.cosmosdb.table.tableservice import TableService
from azure.storage.fileshare import ShareServiceClient
import os
import pandas as pd
import json


# Get the connection string from the environment variable
connection_string = os.getenv('AZURE_STORAGE_CONNECTION_STRING')

# Initialize the Table service
table_service = TableService(connection_string=connection_string)

# Initialize the File Share service
share_service_client = ShareServiceClient.from_connection_string(connection_string)
share_client = share_service_client.get_share_client("taskjsonfiles")

# Name of the table created in Azure portal
table_name = 'ProcessDB'


def list_files_in_directory(directory):
    """
    List all files in the given directory within the Azure File Share.

    Args:
        directory (str): The directory within the file share to list files from.

    Returns:
        list: List of file names in the specified directory.
    """
    try:
        file_list = share_client.list_directories_and_files(directory)
        files = [file.name for file in file_list]
        return files
    except Exception as e:
        raise Exception(f"Error listing files in directory: {e}")


def download_file_from_azure(file_path):
    """
    Download a file from the Azure File Share.

    Args:
        file_path (str): The path of the file to be downloaded within the file share.

    Returns:
        dict: The JSON content of the downloaded file.

    Raises:
        Exception: If the file cannot be downloaded or parsed.
    """
    try:
        file_client = share_client.get_file_client(file_path)
        download = file_client.download_file()
        downloaded_bytes = download.readall()

        # Assuming the file is JSON, parse it.
        try:
            file_content_json = json.loads(downloaded_bytes)
            return file_content_json
        except json.JSONDecodeError:
            raise SyntaxError("Invalid JSON")
    except Exception as e:
        raise Exception(f"Error downloading file: {e}")


def clean_and_rename_dataframe(df):
    """
    Clean and rename columns in the DataFrame.

    Args:
        df (pd.DataFrame): The DataFrame to clean and rename.

    Returns:
        pd.DataFrame: The cleaned and renamed DataFrame.
    """
    # Rename the columns
    df = df.rename(columns={'PartitionKey': 'Customer Name', 'RowKey': 'Process Name'})

    # Remove the unwanted columns
    df = df.drop(columns=['LastEditedTime', 'Timestamp', 'etag'])

    return df


def import_table_as_df(table_service, table_name):
    """
    Import an Azure Table as a pandas DataFrame.

    Args:
        table_service (TableService): The Azure Table service client.
        table_name (str): The name of the table to import.

    Returns:
        pd.DataFrame: The table data as a pandas DataFrame.
    """
    entities = []
    marker = None

    while True:
        batch = table_service.query_entities(table_name, marker=marker)
        entities.extend(batch.items)
        marker = batch.next_marker

        if not marker:
            break

    # Convert the list of entities to a pandas DataFrame
    df = pd.DataFrame(entities)
    df = clean_and_rename_dataframe(df)

    return df


def return_row_df(df, customer_name, process_name):
    """
    Retrieve a row from the DataFrame that matches the given customer name and process name.

    Args:
        df (pd.DataFrame): The DataFrame to search.
        customer_name (str): The customer name to search for.
        process_name (str): The process name to search for.

    Returns:
        pd.Series: The matching row from the DataFrame.

    Raises:
        ValueError: If no matching row is found or multiple matching rows are found.
    """
    # Convert to lowercase for case-insensitive comparison
    customer_name_lower = customer_name.lower()
    process_name_lower = process_name.lower()

    # Define the conditions for substring matching in a case-insensitive manner
    condition1 = df['Customer Name'].str.lower().str.contains(customer_name_lower)
    condition2 = df['Process Name'].str.lower().str.contains(process_name_lower)

    # Combine the conditions
    combined_condition = condition1 & condition2

    # Filter the DataFrame based on the combined condition
    matching_rows = df[combined_condition]

    if len(matching_rows) == 1:
        return matching_rows.iloc[0], True
    else:
        return None, False


def load_task_data(customer_name, process_name):
    """
    Load task data for a specific customer and process.

    Args:
        customer_name (str): The name of the customer.
        process_name (str): The name of the process.

    Returns:
        dict: The task data as a JSON object.
    """
    # Import the table as a DataFrame
    df = import_table_as_df(table_service, table_name)

    # Find the matching row
    process_row, found = return_row_df(df, customer_name, process_name)

    if found:
        # Get the task data URL
        task_data_url = process_row['TaskDescriptionURL']

        # Extract the file path from the URL
        file_path = 'jsonfiles/' + task_data_url.split('/')[-1]

        # Download the task data file
        task_file = download_file_from_azure(file_path)

        return process_row, task_file, found

    else:
        return process_row, None, found


if __name__ == '__main__':
    # Load task data for the specified customer and process
    process_row, task_data, found = load_task_data('Nieuwe Stroom', 'Nieuwe-Stroom-MinderNL-Main')

    print(found)

