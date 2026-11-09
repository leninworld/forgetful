"""Detached, operation-specific Task Plugin contexts and transient write results."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.plan_models import (
    Criterion, CriterionCreate, CriterionUpdate, Plan, Task, TaskClaimRequest,
    TaskCreate, TaskDependency, TaskDependencyRequest, TaskState,
    TaskTransitionRequest, TaskUpdate,
)
from app.models.write_plugin_models import PluginWarning

TaskOperation = Literal[
    "create", "update", "delete", "claim", "transition",
    "criterion_add", "criterion_update", "criterion_delete",
    "dependency_add", "dependency_remove",
]


class TaskContext(BaseModel):
    user_id: UUID
    plan: Plan
    origin: Literal["direct", "inline_create"] = "direct"


class TaskCreateRequest(TaskContext):
    operation: Literal["create"] = "create"
    submitted: TaskCreate
    proposed: TaskCreate


class TaskUpdateRequest(TaskContext):
    operation: Literal["update"] = "update"
    task_id: int
    submitted: TaskUpdate
    previous: Task
    proposed: Task


class TaskDeleteRequest(TaskContext):
    operation: Literal["delete"] = "delete"
    task_id: int
    previous: Task


class TaskClaimRequestContext(TaskContext):
    operation: Literal["claim"] = "claim"
    task_id: int
    submitted: TaskClaimRequest
    previous: Task
    proposed_state: Literal["doing"] = "doing"


class TaskTransitionRequestContext(TaskContext):
    operation: Literal["transition"] = "transition"
    task_id: int
    submitted: TaskTransitionRequest
    previous: Task
    proposed_state: TaskState


class CriterionAddRequest(TaskContext):
    operation: Literal["criterion_add"] = "criterion_add"
    # Inline creation has a draft, with no invented database ID or timestamps.
    parent_task: Task | TaskCreate
    submitted: CriterionCreate
    proposed: CriterionCreate


class CriterionState(BaseModel):
    description: str | None
    met: bool | None


class CriterionUpdateRequest(TaskContext):
    operation: Literal["criterion_update"] = "criterion_update"
    parent_task: Task
    criterion_id: int
    submitted: CriterionUpdate
    previous: Criterion
    proposed: CriterionState


class CriterionDeleteRequest(TaskContext):
    operation: Literal["criterion_delete"] = "criterion_delete"
    parent_task: Task
    criterion_id: int
    previous: Criterion


class DependencyAddRequest(TaskContext):
    operation: Literal["dependency_add"] = "dependency_add"
    parent_task: Task | TaskCreate
    target_task: Task
    submitted: TaskDependencyRequest
    previously_linked: bool
    proposed_linked: Literal[True] = True


class DependencyRemoveRequest(TaskContext):
    operation: Literal["dependency_remove"] = "dependency_remove"
    parent_task: Task
    target_task: Task
    submitted: TaskDependencyRequest
    previously_linked: Literal[True] = True
    proposed_linked: Literal[False] = False


TaskBeforeRequest = Annotated[
    TaskCreateRequest | TaskUpdateRequest | TaskDeleteRequest |
    TaskClaimRequestContext | TaskTransitionRequestContext |
    CriterionAddRequest | CriterionUpdateRequest | CriterionDeleteRequest |
    DependencyAddRequest | DependencyRemoveRequest,
    Field(discriminator="operation"),
]


class TaskSavedRequest(TaskContext):
    operation: Literal["create", "update", "claim", "transition"]
    previous: Task | None
    saved: Task


class TaskDeletedRequest(TaskContext):
    operation: Literal["delete"] = "delete"
    previous: Task
    deleted_task_id: int


class CriterionSavedRequest(TaskContext):
    operation: Literal["criterion_add", "criterion_update"]
    parent_task: Task
    previous: Criterion | None
    saved: Criterion


class CriterionDeletedRequest(TaskContext):
    operation: Literal["criterion_delete"] = "criterion_delete"
    parent_task: Task
    previous: Criterion
    deleted_criterion_id: int


class DependencySavedRequest(TaskContext):
    operation: Literal["dependency_add"] = "dependency_add"
    parent_task: Task
    target_task: Task
    saved: TaskDependency


class DependencyRemovedRequest(TaskContext):
    operation: Literal["dependency_remove"] = "dependency_remove"
    parent_task: Task
    target_task: Task
    removed_task_id: int
    removed_dependency_task_id: int


TaskAfterRequest = Annotated[
    TaskSavedRequest | TaskDeletedRequest | CriterionSavedRequest |
    CriterionDeletedRequest | DependencySavedRequest | DependencyRemovedRequest,
    Field(discriminator="operation"),
]


class TaskWriteResult(BaseModel):
    task: Task | None
    plugin_warnings: list[PluginWarning] = Field(default_factory=list)


class CriterionWriteResult(BaseModel):
    criterion: Criterion
    plugin_warnings: list[PluginWarning] = Field(default_factory=list)


class DependencyWriteResult(BaseModel):
    dependency: TaskDependency
    plugin_warnings: list[PluginWarning] = Field(default_factory=list)


class TaskRemoveResult(BaseModel):
    success: bool
    plugin_warnings: list[PluginWarning] = Field(default_factory=list)
