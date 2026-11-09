"""Trusted administrator example: explicit Task Plugin transition policies."""

from app.models.task_plugin_models import TaskAfterRequest, TaskBeforeRequest
from app.models.write_plugin_models import PluginWarning, WriteDecision
from app.protocols.task_plugin_protocol import TaskPlugin


async def before_transition(request: TaskBeforeRequest) -> WriteDecision:
    if request.operation == "transition" and request.proposed_state == "done":
        if not request.previous.assigned_agent:
            return WriteDecision(
                outcome="reject", message="Assign this task before completing it.", code="TASK_ASSIGNMENT_REQUIRED",
            )
    return WriteDecision(outcome="allow")


async def after_transition(request: TaskAfterRequest) -> PluginWarning | None:
    if request.operation == "transition" and request.saved.state == "done":
        return PluginWarning(message="Record the completion in your team system.", code="TASK_COMPLETION_NOTE")
    return None


plugin = TaskPlugin(
    before_save={"transition": before_transition}, after_save={"transition": after_transition},
)
