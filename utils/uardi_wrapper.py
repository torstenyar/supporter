import os
from azure.cosmos import CosmosClient
from dotenv import load_dotenv

load_dotenv()


class UARDIWrapper:
    def __init__(self):
        endpoint = os.environ['COSMOS_ENDPOINT']
        key = os.environ['COSMOS_KEY']
        self.client = CosmosClient(endpoint, key)
        self.database = self.client.get_database_client('YaradoAIDB')


class MainTaskWrapper(UARDIWrapper):
    def __init__(self):
        super().__init__()
        self.container = self.database.get_container_client('MainTaskContainer')

    async def get_main_task(self, organisation_name: str, task_name: str):
        query = """SELECT * FROM c WHERE c.organisation_name = @organisation_name AND c.task_name = @task_name
        """
        parameters = [
            {"name": "@organisation_name", "value": organisation_name},
            {"name": "@task_name", "value": task_name}
        ]
        results = self.container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True)

        return next(results, None)


class StepsWrapper(UARDIWrapper):
    def __init__(self):
        super().__init__()
        self.container = self.database.get_container_client('StepsContainer')

    async def get_step(self, organisation_id: str, step_id: str):
        try:
            query = """
            SELECT c.id, c.coords, c.name, c.type, c.payload, c.ai_description
            FROM c 
            WHERE c.id = @step_id 
            AND STARTSWITH(c.organisationID_taskname, @organisation_id)
            """
            parameters = [
                {"name": "@step_id", "value": step_id},
                {"name": "@organisation_id", "value": organisation_id}
            ]
            results = list(self.container.query_items(
                query=query,
                parameters=parameters,
                enable_cross_partition_query=True
            ))

            if results:
                return results[0]
            else:
                return None
        except Exception as e:
            print(f"Error fetching step: {e}")
            return None


class ResolvedErrorWrapper(UARDIWrapper):
    def __init__(self):
        super().__init__()
        self.container = self.database.get_container_client('ResolvedContainer')

    async def get_resolved_errors(self, organisation_id: str, step_id: str):
        query = """
        SELECT * FROM c 
        WHERE c.organisation_id = @organisation_id 
        AND c.step_id_pof = @step_id
        ORDER BY c.datetime_of_resolved DESC
        """
        parameters = [
            {"name": "@organisation_id", "value": organisation_id},
            {"name": "@step_id", "value": step_id}
        ]
        results = list(self.container.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True
        ))
        return results

    async def get_resolved_errors_by_task_run_ids(self, task_run_ids):
        # Ensure task_run_ids is a list and contains at least one element
        if not isinstance(task_run_ids, list) or len(task_run_ids) == 0:
            raise ValueError("task_run_ids must be a non-empty list")

        # Join the list of task_run_ids into a comma-separated string of quoted values
        task_run_ids_str = ", ".join(f"'{task_run_id}'" for task_run_id in task_run_ids)

        # Construct the query string
        query = f"""
        SELECT * FROM c 
        WHERE c.task_run_id IN ({task_run_ids_str})
        """

        print(f"Executing query with task_run_ids: {task_run_ids_str}")

        results = list(self.container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))

        print(f"Query returned {len(results)} results")

        return results

    async def get_all_resolved_errors(self):
        query = """
        SELECT *
        FROM c
        WHERE IS_DEFINED(c.supporter_rate)
        AND IS_DEFINED(c.supporter_feedback)
        AND IS_DEFINED(c.supporter_reason)
        """
        errors = list(self.container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))
        return errors
