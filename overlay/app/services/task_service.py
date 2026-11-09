from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

from app.config.logging_config import logging
from app.exceptions import (
    ConflictError,
    CyclicDependencyError,
    DependencyNotMetError,
    InvalidStateTransitionError,
    NotFoundError,
    PlanPluginRejectedError,
    PlanPluginStateChangedError,
    PlanPluginUnavailableError,
    TaskPluginStateChangedError,
)
from app.models.activity_models import (
    ActionType,
    ActivityEvent,
    ActorType,
    EntityType,
)
from app.models.plan_models import (
    VALID_TASK_TRANSITIONS,
    Criterion,
    CriterionCreate,
    CriterionUpdate,
    PlanStatus,
    PlanUpdate,
    Task,
    TaskCreate,
    TaskPriority,
    TaskState,
    TaskSummary,
    TaskUpdate,
)
from app.models.plan_plugin_models import TaskDeleteResult, TaskTransitionResult
from app.models import task_plugin_models as tp
from app.models.write_plugin_models import PluginWarning
from app.protocols.task_protocol import TaskRepository
from app.services.plan_service import PlanService
from app.utils.provenance import (
    apply_provenance_defaults,
    apply_provenance_defaults_for_update,
)
from app.utils.pydantic_helper import get_changed_fields

if TYPE_CHECKING:
    from app.events import EventBus
    from app.services.task_write_plugin_service import TaskWritePluginService

logger = logging.getLogger(__name__)


class TaskService:
    """Service layer for task operations with state machine enforcement."""

    def __init__(
        self,
        task_repo: TaskRepository,
        plan_service: PlanService,
        event_bus: "EventBus | None" = None,
        task_plugin: "TaskWritePluginService | None" = None,
    ):
        self.task_repo = task_repo
        self.plan_service = plan_service
        self._event_bus = event_bus
        self._task_plugin = task_plugin
        logger.info("Task service initialised")

    async def _emit_event(
        self,
        user_id: UUID,
        entity_type: EntityType,
        entity_id: int,
        action: ActionType,
        snapshot: dict,
        changes: dict | None = None,
        metadata: dict | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        event = ActivityEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            changes=changes,
            snapshot=snapshot,
            actor=ActorType.USER,
            metadata=metadata,
            user_id=str(user_id),
        )
        await self._event_bus.emit(event)

    async def create_task(self, user_id: UUID, task_data: TaskCreate) -> tp.TaskWriteResult:
        """Create a task; policy preflight never holds a database session open."""
        submitted = task_data.model_copy(deep=True)
        plan = await self.plan_service.get_plan(user_id=user_id, plan_id=task_data.plan_id)
        self._validate_create_plan(plan)
        task_data = apply_provenance_defaults(task_data)
        warnings: list[PluginWarning] = []
        preflight = (self._has_before("create")
                     or bool(task_data.criteria) and self._has_before("criterion_add")
                     or bool(task_data.dependency_ids) and self._has_before("dependency_add"))
        if preflight:
            await self._validate_inline_dependencies(user_id, task_data)
            if self._has_before("create"):
                warnings.extend(await self._task_plugin.before_save(tp.TaskCreateRequest(
                    user_id=user_id, plan=plan.model_copy(deep=True),
                    submitted=submitted, proposed=task_data.model_copy(deep=True),
                )))
            for item in task_data.criteria or []:
                if self._has_before("criterion_add"):
                    warnings.extend(await self._task_plugin.before_save(tp.CriterionAddRequest(
                        user_id=user_id, plan=plan.model_copy(deep=True), origin="inline_create",
                        parent_task=task_data.model_copy(deep=True), submitted=item.model_copy(deep=True),
                        proposed=item.model_copy(deep=True),
                    )))
            for target_id in task_data.dependency_ids or []:
                if self._has_before("dependency_add"):
                    target = await self.get_task(user_id, target_id)
                    warnings.extend(await self._task_plugin.before_save(tp.DependencyAddRequest(
                        user_id=user_id, plan=plan.model_copy(deep=True), origin="inline_create",
                        parent_task=task_data.model_copy(deep=True), target_task=target,
                        submitted=tp.TaskDependencyRequest(depends_on_task_id=target_id),
                        previously_linked=False,
                    )))
            # Recheck all core references after the last handler, before the first write.
            plan = await self.plan_service.get_plan(user_id=user_id, plan_id=task_data.plan_id)
            self._validate_create_plan(plan)
            await self._validate_inline_dependencies(user_id, task_data)

        task = await self.task_repo.create_task(user_id=user_id, task_data=task_data)
        criteria = []
        dependencies = []
        for item in task_data.criteria or []:
            criteria.append(await self.task_repo.create_criterion(
                user_id=user_id, task_id=task.id, criterion_data=item,
            ))
        for target_id in task_data.dependency_ids or []:
            # Preserve the existing post-create validation/error timing without a before policy.
            await self._validate_same_plan(user_id, task.id, target_id, task.plan_id)
            await self._validate_no_cycle(user_id, task.id, target_id)
            dependencies.append(await self.task_repo.add_dependency(
                user_id=user_id, task_id=task.id, depends_on_task_id=target_id,
            ))
        task = await self.get_task(user_id, task.id)
        await self._emit_event(
            user_id=user_id, entity_type=EntityType.TASK, entity_id=task.id,
            action=ActionType.CREATED, snapshot=task.model_dump(mode="json"),
        )
        if self._has_after("create"):
            warnings.extend(await self._task_plugin.after_save(tp.TaskSavedRequest(
                operation="create", user_id=user_id, plan=plan, previous=None, saved=task,
            )))
        for criterion in criteria:
            if self._has_after("criterion_add"):
                warnings.extend(await self._task_plugin.after_save(tp.CriterionSavedRequest(
                    operation="criterion_add", user_id=user_id, plan=plan, origin="inline_create",
                    parent_task=task, previous=None, saved=criterion,
                )))
        for dependency in dependencies:
            if self._has_after("dependency_add"):
                # The endpoint snapshot is for context; its disappearance must not fail a saved write.
                try:
                    target = await self.get_task(user_id, dependency.depends_on_task_id)
                    warnings.extend(await self._task_plugin.after_save(tp.DependencySavedRequest(
                        user_id=user_id, plan=plan, origin="inline_create", parent_task=task,
                        target_task=target, saved=dependency,
                    )))
                except NotFoundError as exc:
                    warnings.append(self._task_plugin.after_save_unavailable("dependency_add", exc))
        return tp.TaskWriteResult(task=task, plugin_warnings=warnings)

    async def get_task(self, user_id: UUID, task_id: int) -> Task:
        logger.info("getting task", extra={"user_id": str(user_id), "task_id": task_id})
        task = await self.task_repo.get_task_by_id(user_id=user_id, task_id=task_id)
        if task:
            logger.info("task retrieved", extra={"task_id": task_id})
            return task
        logger.info("task not found", extra={"task_id": task_id})
        raise NotFoundError(f"Task with id {task_id} not found")

    async def list_tasks(
        self,
        user_id: UUID,
        plan_id: int,
        state: TaskState | None = None,
        priority: TaskPriority | None = None,
        assigned_agent: str | None = None,
    ) -> list[TaskSummary]:
        logger.info("listing tasks", extra={"plan_id": plan_id})
        tasks = await self.task_repo.list_tasks(
            user_id=user_id, plan_id=plan_id,
            state=state, priority=priority, assigned_agent=assigned_agent,
        )
        logger.info("tasks retrieved", extra={"count": len(tasks)})
        return tasks

    async def list_tasks_for_user(
        self,
        user_id: UUID,
        plan_ids: list[int] | None = None,
    ) -> list[TaskSummary]:
        logger.info(
            "listing tasks for user",
            extra={"user_id": str(user_id), "plan_ids_count": len(plan_ids) if plan_ids is not None else None},
        )
        tasks = await self.task_repo.list_tasks_for_user(user_id=user_id, plan_ids=plan_ids)
        logger.info("tasks retrieved", extra={"count": len(tasks)})
        return tasks

    async def update_task(
        self, user_id: UUID, task_id: int, task_data: TaskUpdate,
    ) -> tp.TaskWriteResult:
        submitted = task_data.model_copy(deep=True)
        task_data = apply_provenance_defaults_for_update(task_data)
        existing = await self.task_repo.get_task_by_id(user_id=user_id, task_id=task_id)
        if existing is None:
            return tp.TaskWriteResult(task=None)
        changed_fields = get_changed_fields(input_model=task_data, existing_model=existing)
        if not changed_fields:
            return tp.TaskWriteResult(task=existing)
        warnings: list[PluginWarning] = []
        plan = await self._context_plan(user_id, existing.plan_id, "update")
        if self._has_before("update"):
            warnings.extend(await self._task_plugin.before_save(tp.TaskUpdateRequest(
                user_id=user_id, plan=plan, task_id=task_id, submitted=submitted,
                previous=existing.model_copy(deep=True),
                proposed=existing.model_copy(update={k: pair[1] for k, pair in changed_fields.items()}, deep=True),
            )))
            current = await self.task_repo.get_task_by_id(user_id=user_id, task_id=task_id)
            if current is None:
                return tp.TaskWriteResult(task=None, plugin_warnings=warnings)
            self._same_task(existing, current)
            existing = current
            changed_fields = get_changed_fields(input_model=task_data, existing_model=existing)
            if not changed_fields:
                return tp.TaskWriteResult(task=existing, plugin_warnings=warnings)
        previous = existing.model_copy(deep=True)
        updated = await self.task_repo.update_task(user_id=user_id, task_id=task_id, task_data=task_data)
        await self._emit_event(
            user_id=user_id, entity_type=EntityType.TASK, entity_id=task_id,
            action=ActionType.UPDATED, snapshot=updated.model_dump(mode="json"),
            changes={k: {"old": pair[0], "new": pair[1]} for k, pair in changed_fields.items()},
        )
        if self._has_after("update"):
            warnings.extend(await self._task_plugin.after_save(tp.TaskSavedRequest(
                operation="update", user_id=user_id, plan=plan, previous=previous, saved=updated,
            )))
        return tp.TaskWriteResult(task=updated, plugin_warnings=warnings)

    async def delete_task(self, user_id: UUID, task_id: int) -> TaskDeleteResult:
        existing = await self.task_repo.get_task_by_id(user_id=user_id, task_id=task_id)
        if existing is None:
            return TaskDeleteResult(success=False)
        warnings: list[PluginWarning] = []
        plan = await self._context_plan(user_id, existing.plan_id, "delete", missing_ok=True)
        if self._registered("delete") and plan is None:
            return TaskDeleteResult(success=False)
        if self._has_before("delete"):
            warnings.extend(await self._task_plugin.before_save(tp.TaskDeleteRequest(
                user_id=user_id, plan=plan, task_id=task_id, previous=existing.model_copy(deep=True),
            )))
            current = await self.task_repo.get_task_by_id(user_id=user_id, task_id=task_id)
            if current is None:
                return TaskDeleteResult(success=False, plugin_warnings=warnings)
            self._same_task(existing, current)
            existing = current
        success = await self.task_repo.delete_task(user_id=user_id, task_id=task_id)
        if success:
            await self._emit_event(
                user_id=user_id, entity_type=EntityType.TASK, entity_id=task_id,
                action=ActionType.DELETED, snapshot=existing.model_dump(mode="json"),
            )
            if self._has_after("delete"):
                warnings.extend(await self._task_plugin.after_save(tp.TaskDeletedRequest(
                    user_id=user_id, plan=plan, previous=existing, deleted_task_id=task_id,
                )))
            warnings.extend(await self._check_plan_auto_completion(user_id, existing.plan_id))
        return TaskDeleteResult(success=success, plugin_warnings=warnings)

    async def transition_task(
        self, user_id: UUID, task_id: int, new_state: TaskState, expected_version: int,
    ) -> TaskTransitionResult:
        task = await self.get_task(user_id, task_id)
        await self._validate_transition(user_id, task, new_state, expected_version)
        warnings: list[PluginWarning] = []
        plan = await self._context_plan(user_id, task.plan_id, "transition")
        if self._has_before("transition"):
            warnings.extend(await self._task_plugin.before_save(tp.TaskTransitionRequestContext(
                user_id=user_id, plan=plan, task_id=task_id,
                submitted=tp.TaskTransitionRequest(state=new_state, version=expected_version),
                previous=task.model_copy(deep=True), proposed_state=new_state,
            )))
            current = await self.get_task(user_id, task_id)
            self._same_task(task, current)
            task = current
            await self._validate_transition(user_id, task, new_state, expected_version)
        previous = task.model_copy(deep=True)
        updated = await self.task_repo.transition_task_state(
            user_id=user_id, task_id=task_id, new_state=new_state, expected_version=expected_version,
        )
        await self._emit_event(
            user_id=user_id, entity_type=EntityType.TASK, entity_id=task_id,
            action=ActionType.UPDATED, snapshot=updated.model_dump(mode="json"),
            changes={"state": {"old": previous.state, "new": new_state.value}},
        )
        if self._has_after("transition"):
            warnings.extend(await self._task_plugin.after_save(tp.TaskSavedRequest(
                operation="transition", user_id=user_id, plan=plan, previous=previous, saved=updated,
            )))
        if new_state in (TaskState.DONE, TaskState.CANCELLED):
            warnings.extend(await self._check_plan_auto_completion(user_id, task.plan_id))
        return TaskTransitionResult(task=updated, plugin_warnings=warnings)

    async def claim_task(
        self, user_id: UUID, task_id: int, agent_id: str, expected_version: int,
    ) -> tp.TaskWriteResult:
        task = await self.get_task(user_id, task_id)
        await self._validate_claim(user_id, task, expected_version)
        warnings: list[PluginWarning] = []
        plan = await self._context_plan(user_id, task.plan_id, "claim")
        if self._has_before("claim"):
            warnings.extend(await self._task_plugin.before_save(tp.TaskClaimRequestContext(
                user_id=user_id, plan=plan, task_id=task_id,
                submitted=tp.TaskClaimRequest(agent_id=agent_id, version=expected_version),
                previous=task.model_copy(deep=True),
            )))
            current = await self.get_task(user_id, task_id)
            self._same_task(task, current)
            task = current
            await self._validate_claim(user_id, task, expected_version)
        previous = task.model_copy(deep=True)
        updated = await self.task_repo.transition_task_state(
            user_id=user_id, task_id=task_id, new_state=TaskState.DOING,
            expected_version=expected_version, assigned_agent=agent_id,
        )
        await self._emit_event(
            user_id=user_id, entity_type=EntityType.TASK, entity_id=task_id,
            action=ActionType.UPDATED, snapshot=updated.model_dump(mode="json"),
            changes={"state": {"old": previous.state, "new": TaskState.DOING.value},
                     "assigned_agent": {"old": previous.assigned_agent, "new": agent_id}},
        )
        if self._has_after("claim"):
            warnings.extend(await self._task_plugin.after_save(tp.TaskSavedRequest(
                operation="claim", user_id=user_id, plan=plan, previous=previous, saved=updated,
            )))
        return tp.TaskWriteResult(task=updated, plugin_warnings=warnings)

    # ---- Criteria ----

    async def add_criterion(
        self, user_id: UUID, task_id: int, criterion_data: CriterionCreate,
    ) -> tp.CriterionWriteResult:
        task = await self.get_task(user_id, task_id)
        self._validate_criterion_parent(task)
        warnings: list[PluginWarning] = []
        plan = await self._context_plan(user_id, task.plan_id, "criterion_add")
        if self._has_before("criterion_add"):
            warnings.extend(await self._task_plugin.before_save(tp.CriterionAddRequest(
                user_id=user_id, plan=plan, parent_task=task.model_copy(deep=True),
                submitted=criterion_data.model_copy(deep=True), proposed=criterion_data.model_copy(deep=True),
            )))
            current = await self.get_task(user_id, task_id)
            self._same_task(task, current)
            task = current
            self._validate_criterion_parent(task)
        criterion = await self.task_repo.create_criterion(
            user_id=user_id, task_id=task_id, criterion_data=criterion_data,
        )
        if self._has_after("criterion_add"):
            warnings.extend(await self._task_plugin.after_save(tp.CriterionSavedRequest(
                operation="criterion_add", user_id=user_id, plan=plan, parent_task=task,
                previous=None, saved=criterion,
            )))
        return tp.CriterionWriteResult(criterion=criterion, plugin_warnings=warnings)

    async def update_criterion(
        self, user_id: UUID, criterion_id: int, criterion_data: CriterionUpdate,
    ) -> tp.CriterionWriteResult:
        warnings: list[PluginWarning] = []
        previous = None
        task = None
        plan = None
        registered = self._registered("criterion_update") and bool(criterion_data.model_fields_set)
        if registered:
            previous = await self.task_repo.get_criterion_by_id(user_id, criterion_id)
            if previous is None:
                raise NotFoundError(f"Criterion with id {criterion_id} not found")
            task = await self.get_task(user_id, previous.task_id)
            plan = await self._context_plan(user_id, task.plan_id, "criterion_update")
            if self._has_before("criterion_update"):
                values = {**previous.model_dump(), **criterion_data.model_dump(exclude_unset=True)}
                warnings.extend(await self._task_plugin.before_save(tp.CriterionUpdateRequest(
                    user_id=user_id, plan=plan, parent_task=task.model_copy(deep=True),
                    criterion_id=criterion_id, submitted=criterion_data.model_copy(deep=True),
                    previous=previous.model_copy(deep=True),
                    proposed=tp.CriterionState(description=values["description"], met=values["met"]),
                )))
                current = await self.task_repo.get_criterion_by_id(user_id, criterion_id)
                if current is None:
                    raise NotFoundError(f"Criterion with id {criterion_id} not found")
                self._same_criterion(previous, current)
                current_task = await self.get_task(user_id, current.task_id)
                self._same_task(task, current_task)
                task, previous = current_task, current
        criterion = await self.task_repo.update_criterion(
            user_id=user_id, criterion_id=criterion_id, criterion_data=criterion_data,
        )
        if registered and self._has_after("criterion_update"):
            warnings.extend(await self._task_plugin.after_save(tp.CriterionSavedRequest(
                operation="criterion_update", user_id=user_id, plan=plan,
                parent_task=task, previous=previous, saved=criterion,
            )))
        return tp.CriterionWriteResult(criterion=criterion, plugin_warnings=warnings)

    async def delete_criterion(self, user_id: UUID, criterion_id: int) -> tp.TaskRemoveResult:
        warnings: list[PluginWarning] = []
        previous = None
        task = None
        plan = None
        if self._registered("criterion_delete"):
            previous = await self.task_repo.get_criterion_by_id(user_id, criterion_id)
            if previous is None:
                return tp.TaskRemoveResult(success=False)
            task = await self.task_repo.get_task_by_id(user_id, previous.task_id)
            if task is None:
                return tp.TaskRemoveResult(success=False)
            plan = await self._context_plan(user_id, task.plan_id, "criterion_delete", missing_ok=True)
            if plan is None:
                return tp.TaskRemoveResult(success=False)
            if self._has_before("criterion_delete"):
                warnings.extend(await self._task_plugin.before_save(tp.CriterionDeleteRequest(
                    user_id=user_id, plan=plan, parent_task=task.model_copy(deep=True),
                    criterion_id=criterion_id, previous=previous.model_copy(deep=True),
                )))
                current = await self.task_repo.get_criterion_by_id(user_id, criterion_id)
                if current is None:
                    return tp.TaskRemoveResult(success=False, plugin_warnings=warnings)
                self._same_criterion(previous, current)
                current_task = await self.task_repo.get_task_by_id(user_id, current.task_id)
                if current_task is None:
                    return tp.TaskRemoveResult(success=False, plugin_warnings=warnings)
                self._same_task(task, current_task)
                task, previous = current_task, current
        success = await self.task_repo.delete_criterion(user_id=user_id, criterion_id=criterion_id)
        if success and self._has_after("criterion_delete"):
            warnings.extend(await self._task_plugin.after_save(tp.CriterionDeletedRequest(
                user_id=user_id, plan=plan, parent_task=task,
                previous=previous, deleted_criterion_id=criterion_id,
            )))
        return tp.TaskRemoveResult(success=success, plugin_warnings=warnings)

    # ---- Dependencies ----

    async def add_dependency(
        self, user_id: UUID, task_id: int, depends_on_task_id: int,
    ) -> tp.DependencyWriteResult:
        task = await self.get_task(user_id, task_id)
        target = await self.get_task(user_id, depends_on_task_id)
        await self._validate_same_plan(user_id, task_id, depends_on_task_id, task.plan_id)
        await self._validate_no_cycle(user_id, task_id, depends_on_task_id)
        warnings: list[PluginWarning] = []
        # A duplicate retains the repository's existing constraint failure, without a callback.
        eligible = depends_on_task_id not in task.dependency_ids
        plan = await self._context_plan(user_id, task.plan_id, "dependency_add") if eligible else None
        if eligible and self._has_before("dependency_add"):
            warnings.extend(await self._task_plugin.before_save(tp.DependencyAddRequest(
                user_id=user_id, plan=plan, parent_task=task.model_copy(deep=True),
                target_task=target.model_copy(deep=True),
                submitted=tp.TaskDependencyRequest(depends_on_task_id=depends_on_task_id),
                previously_linked=False,
            )))
            current = await self.get_task(user_id, task_id)
            current_target = await self.get_task(user_id, depends_on_task_id)
            self._same_task(task, current)
            self._same_task(target, current_target)
            task, target = current, current_target
            await self._validate_same_plan(user_id, task_id, depends_on_task_id, task.plan_id)
            await self._validate_no_cycle(user_id, task_id, depends_on_task_id)
        dependency = await self.task_repo.add_dependency(
            user_id=user_id, task_id=task_id, depends_on_task_id=depends_on_task_id,
        )
        if eligible and self._has_after("dependency_add"):
            warnings.extend(await self._task_plugin.after_save(tp.DependencySavedRequest(
                user_id=user_id, plan=plan, parent_task=task, target_task=target, saved=dependency,
            )))
        return tp.DependencyWriteResult(dependency=dependency, plugin_warnings=warnings)

    async def remove_dependency(
        self, user_id: UUID, task_id: int, depends_on_task_id: int,
    ) -> tp.TaskRemoveResult:
        warnings: list[PluginWarning] = []
        task = target = plan = None
        if self._registered("dependency_remove"):
            task = await self.task_repo.get_task_by_id(user_id, task_id)
            target = await self.task_repo.get_task_by_id(user_id, depends_on_task_id)
            if task is None or target is None or depends_on_task_id not in task.dependency_ids:
                return tp.TaskRemoveResult(success=False)
            plan = await self._context_plan(user_id, task.plan_id, "dependency_remove", missing_ok=True)
            if plan is None:
                return tp.TaskRemoveResult(success=False)
            if self._has_before("dependency_remove"):
                warnings.extend(await self._task_plugin.before_save(tp.DependencyRemoveRequest(
                    user_id=user_id, plan=plan, parent_task=task.model_copy(deep=True),
                    target_task=target.model_copy(deep=True),
                    submitted=tp.TaskDependencyRequest(depends_on_task_id=depends_on_task_id),
                )))
                current = await self.task_repo.get_task_by_id(user_id, task_id)
                current_target = await self.task_repo.get_task_by_id(user_id, depends_on_task_id)
                if current is None or current_target is None or depends_on_task_id not in current.dependency_ids:
                    return tp.TaskRemoveResult(success=False, plugin_warnings=warnings)
                self._same_task(task, current)
                self._same_task(target, current_target)
                task, target = current, current_target
        success = await self.task_repo.remove_dependency(
            user_id=user_id, task_id=task_id, depends_on_task_id=depends_on_task_id,
        )
        if success and self._has_after("dependency_remove"):
            warnings.extend(await self._task_plugin.after_save(tp.DependencyRemovedRequest(
                user_id=user_id, plan=plan, parent_task=task, target_task=target,
                removed_task_id=task_id, removed_dependency_task_id=depends_on_task_id,
            )))
        return tp.TaskRemoveResult(success=success, plugin_warnings=warnings)

    # ---- Private Helpers ----

    def _has_before(self, operation: tp.TaskOperation) -> bool:
        return self._task_plugin is not None and self._task_plugin.has_before(operation)

    def _has_after(self, operation: tp.TaskOperation) -> bool:
        return self._task_plugin is not None and self._task_plugin.has_after(operation)

    def _registered(self, operation: tp.TaskOperation) -> bool:
        return self._has_before(operation) or self._has_after(operation)

    async def _context_plan(
        self, user_id: UUID, plan_id: int, operation: tp.TaskOperation, *, missing_ok: bool = False,
    ):
        if not self._registered(operation):
            return None
        try:
            return await self.plan_service.get_plan(user_id=user_id, plan_id=plan_id)
        except NotFoundError:
            if missing_ok:
                return None
            raise

    @staticmethod
    def _same_task(previous: Task, current: Task) -> None:
        if previous.plan_id != current.plan_id or previous.created_at != current.created_at:
            raise TaskPluginStateChangedError("Task changed while task policy was evaluated.")

    @staticmethod
    def _same_criterion(previous, current) -> None:
        if previous.task_id != current.task_id or previous.created_at != current.created_at:
            raise TaskPluginStateChangedError("Criterion changed while task policy was evaluated.")

    @staticmethod
    def _validate_create_plan(plan) -> None:
        status = PlanStatus(plan.status)
        if status in (PlanStatus.COMPLETED, PlanStatus.ARCHIVED):
            raise InvalidStateTransitionError(f"Cannot add tasks to plan in {status.value} status")

    async def _validate_inline_dependencies(self, user_id: UUID, data: TaskCreate) -> None:
        for target_id in data.dependency_ids or []:
            target = await self.get_task(user_id, target_id)
            if target.plan_id != data.plan_id:
                raise ValueError(f"Dependency task {target_id} is not in the same plan")

    @staticmethod
    def _validate_criterion_parent(task: Task) -> None:
        if TaskState(task.state) == TaskState.DONE:
            raise InvalidStateTransitionError("Cannot add criteria to a completed task")

    @staticmethod
    def _validate_version(task: Task, expected_version: int) -> None:
        if task.version != expected_version:
            raise ConflictError(
                f"Version mismatch: expected {expected_version}, got {task.version}. "
                "Task was modified by another agent.",
            )

    async def _validate_transition(
        self, user_id: UUID, task: Task, new_state: TaskState, expected_version: int,
    ) -> None:
        self._validate_version(task, expected_version)
        state = TaskState(task.state)
        targets = VALID_TASK_TRANSITIONS.get(state, set())
        if new_state not in targets:
            raise InvalidStateTransitionError(
                f"Cannot transition task from {state.value} to {new_state.value}. "
                f"Valid transitions: {[s.value for s in targets]}",
            )
        if new_state == TaskState.DOING:
            await self._validate_dependencies_met(user_id, task)
        if new_state == TaskState.DONE:
            await self._validate_all_criteria_met(user_id, task)

    async def _validate_claim(self, user_id: UUID, task: Task, expected_version: int) -> None:
        self._validate_version(task, expected_version)
        state = TaskState(task.state)
        if state not in (TaskState.TODO, TaskState.WAITING):
            raise InvalidStateTransitionError(
                f"Cannot claim task in {state.value} state. Must be todo or waiting.",
            )
        await self._validate_dependencies_met(user_id, task)

    async def _validate_dependencies_met(self, user_id: UUID, task: Task) -> None:
        """Check all dependencies are in done state."""
        dep_ids = task.dependency_ids
        if not dep_ids:
            return

        for dep_id in dep_ids:
            dep_task = await self.task_repo.get_task_by_id(user_id=user_id, task_id=dep_id)
            if not dep_task or TaskState(dep_task.state) != TaskState.DONE:
                raise DependencyNotMetError(
                    f"Dependency task {dep_id} is not done "
                    f"(state: {dep_task.state if dep_task else 'not found'})",
                )

    async def _validate_all_criteria_met(self, user_id: UUID, task: Task) -> None:
        """Check all criteria have met=True. Tasks with zero criteria CAN be marked done."""
        criteria = await self.task_repo.get_criteria_for_task(user_id=user_id, task_id=task.id)
        if not criteria:
            return  # Zero criteria = can be done

        unmet = [c for c in criteria if not c.met]
        if unmet:
            raise InvalidStateTransitionError(
                f"Cannot mark task as done: {len(unmet)} acceptance criteria not met. "
                f"Unmet: {[c.description[:50] for c in unmet]}",
            )

    async def _validate_no_cycle(self, user_id: UUID, task_id: int, new_dep_id: int) -> None:
        """BFS from new_dep_id following existing 'depends_on' edges.
        If we reach task_id, adding this edge would create a cycle.
        """
        if task_id == new_dep_id:
            raise CyclicDependencyError("A task cannot depend on itself")

        visited: set[int] = set()
        queue: list[int] = [new_dep_id]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            deps = await self.task_repo.get_dependencies(user_id, current)
            for dep_id in deps:
                if dep_id == task_id:
                    raise CyclicDependencyError(
                        f"Adding dependency {task_id} -> {new_dep_id} would create a cycle "
                        f"(path exists from {new_dep_id} back to {task_id})",
                    )
                queue.append(dep_id)

    async def _validate_same_plan(
        self, user_id: UUID, task_id: int, dep_task_id: int, expected_plan_id: int,
    ) -> None:
        """Validate that the dependency task belongs to the same plan."""
        dep_task = await self.task_repo.get_task_by_id(user_id=user_id, task_id=dep_task_id)
        if not dep_task:
            raise NotFoundError(f"Task with id {dep_task_id} not found")
        if dep_task.plan_id != expected_plan_id:
            raise ValueError(
                f"Task {dep_task_id} belongs to plan {dep_task.plan_id}, "
                f"not plan {expected_plan_id}. Dependencies must be within the same plan.",
            )

    async def _plan_tasks_allow_completion(self, user_id: UUID, plan_id: int) -> bool:
        tasks = await self.task_repo.list_tasks(user_id=user_id, plan_id=plan_id)
        if not tasks:
            return False
        all_terminal = all(
            TaskState(t.state) in (TaskState.DONE, TaskState.CANCELLED)
            for t in tasks
        )
        has_done = any(TaskState(t.state) == TaskState.DONE for t in tasks)
        return all_terminal and has_done

    async def _check_plan_auto_completion(
        self, user_id: UUID, plan_id: int,
    ) -> list[PluginWarning]:
        """Complete an ACTIVE parent through plan policy after the task committed.

        Policy failure is advisory for this secondary mutation. Other failures
        retain existing propagation, even though the task has already committed.
        The plan repository has no CAS: concurrent completions can both dispatch.
        Rechecking tasks after policy narrows the race window but is not atomic.
        """
        if not await self._plan_tasks_allow_completion(user_id, plan_id):
            return []
        try:
            plan = await self.plan_service.get_plan(user_id=user_id, plan_id=plan_id)
        except NotFoundError:
            return []
        if PlanStatus(plan.status) != PlanStatus.ACTIVE:
            return []
        logger.info("auto-completing plan", extra={"plan_id": plan_id})
        try:
            result = await self.plan_service.update_plan(
                user_id=user_id, plan_id=plan_id,
                plan_data=PlanUpdate(status=PlanStatus.COMPLETED), automatic=True,
                completion_guard=partial(self._plan_tasks_allow_completion, user_id, plan_id),
            )
        except PlanPluginRejectedError:
            return [PluginWarning(
                code="plan_auto_completion_rejected",
                message="The task change succeeded, but automatic plan completion was rejected by plan policy.",
            )]
        except (PlanPluginUnavailableError, PlanPluginStateChangedError):
            return [PluginWarning(
                code="plan_auto_completion_unavailable",
                message="The task change succeeded, but automatic plan completion was unavailable.",
            )]
        return result.plugin_warnings
