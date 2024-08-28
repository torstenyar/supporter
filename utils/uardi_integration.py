# utils/uardi_integration.py

from typing import Dict, Any, Optional
from .uardi_wrapper import MainTaskWrapper, StepsWrapper, ResolvedErrorWrapper
import json


async def get_uardi_context(organisation_name: str, task_name: str, step_ids: list[str], failed_step_id: str) -> Dict[str, Any]:
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
