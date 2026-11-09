"""Task policy contracts shared by native SQLite and PostgreSQL."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.exceptions import (
    ConflictError, CyclicDependencyError, DependencyNotMetError, InvalidStateTransitionError, NotFoundError,
    TaskPluginRejectedError,
)
from app.models.plan_models import (
    CriterionCreate, CriterionUpdate, PlanStatus, PlanUpdate, TaskCreate, TaskState, TaskUpdate,
)
from app.models.write_plugin_models import PluginWarning, WriteDecision
from app.protocols.task_plugin_protocol import TaskPlugin
from app.services.task_write_plugin_service import TaskWritePluginService
from tests.plan_plugin_cases import active_plan

OPERATIONS = (
    "create", "update", "delete", "claim", "transition", "criterion_add", "criterion_update",
    "criterion_delete", "dependency_add", "dependency_remove",
)


@pytest.fixture
async def task_store(plan_store):
    s = plan_store
    s.plan = await active_plan(s)
    s.task_state = SimpleNamespace(before=[], after=[], outcome="allow", after_error=False, pause=None)

    async def before(request):
        s.task_state.before.append(request.model_copy(deep=True))
        if s.task_state.pause is not None:
            await s.task_state.pause(request)
        return WriteDecision(outcome=s.task_state.outcome, message="Synthetic task policy", code="TASK_TEST")

    async def after(request):
        s.task_state.after.append(request.model_copy(deep=True))
        if s.task_state.after_error:
            raise RuntimeError("SYNTHETIC_PRIVATE_TASK_ERROR")
        return PluginWarning(message="Synthetic saved task warning", code="TASK_AFTER")

    s.task_registration = TaskPlugin(
        before_save=dict.fromkeys(OPERATIONS, before), after_save=dict.fromkeys(OPERATIONS, after),
    )
    s.tasks._task_plugin = TaskWritePluginService(s.task_registration)
    return s


async def prepare(s, operation):
    """Seed through the real repositories; test the requested service operation once."""
    source = None
    peer = None
    criterion = None
    if operation != "create":
        source = await s.repos["task"].create_task(s.user.id, TaskCreate(plan_id=s.plan.id, title="Target"))
    if operation in ("criterion_update", "criterion_delete"):
        criterion = await s.repos["task"].create_criterion(
            s.user.id, source.id, CriterionCreate(description="Stored criterion"),
        )
    if operation in ("dependency_add", "dependency_remove"):
        peer = await s.repos["task"].create_task(s.user.id, TaskCreate(plan_id=s.plan.id, title="Peer"))
        if operation == "dependency_remove":
            await s.repos["task"].add_dependency(s.user.id, source.id, peer.id)

    async def invoke():
        if operation == "create":
            return await s.tasks.create_task(s.user.id, TaskCreate(plan_id=s.plan.id, title="Created"))
        if operation == "update":
            return await s.tasks.update_task(s.user.id, source.id, TaskUpdate(title="Updated"))
        if operation == "delete":
            return await s.tasks.delete_task(s.user.id, source.id)
        if operation == "claim":
            return await s.tasks.claim_task(s.user.id, source.id, "synthetic-agent", source.version)
        if operation == "transition":
            return await s.tasks.transition_task(s.user.id, source.id, TaskState.DOING, source.version)
        if operation == "criterion_add":
            return await s.tasks.add_criterion(s.user.id, source.id, CriterionCreate(description="Added"))
        if operation == "criterion_update":
            return await s.tasks.update_criterion(s.user.id, criterion.id, CriterionUpdate(met=True))
        if operation == "criterion_delete":
            return await s.tasks.delete_criterion(s.user.id, criterion.id)
        if operation == "dependency_add":
            return await s.tasks.add_dependency(s.user.id, source.id, peer.id)
        return await s.tasks.remove_dependency(s.user.id, source.id, peer.id)

    async def snapshot():
        summaries = await s.repos["task"].list_tasks_for_user(s.user.id)
        rows = []
        for summary in summaries:
            task = await s.repos["task"].get_task_by_id(s.user.id, summary.id)
            rows.append(task.model_dump(mode="json"))
        return rows

    return SimpleNamespace(invoke=invoke, snapshot=snapshot, task=source, criterion=criterion, peer=peer)


class TestTaskPluginPersistence:
    @pytest.mark.parametrize("operation", OPERATIONS)
    @pytest.mark.parametrize("outcome,mode", [
        ("allow", "enforce"), ("warn", "enforce"), ("reject", "enforce"), ("reject", "warn"),
    ])
    async def test_all_operations_persist_or_reject_with_detached_outcomes(self, task_store, operation, outcome, mode):
        s = task_store
        case = await prepare(s, operation)
        before = await case.snapshot()
        s.task_state.outcome = outcome
        s.tasks._task_plugin = TaskWritePluginService(s.task_registration, mode=mode)
        if outcome == "reject" and mode == "enforce":
            with pytest.raises(TaskPluginRejectedError):
                await case.invoke()
            assert await case.snapshot() == before
            assert s.task_state.after == []
        else:
            result = await case.invoke()
            assert await case.snapshot() != before
            codes = [w.code for w in result.plugin_warnings]
            assert codes == (["TASK_TEST", "TASK_AFTER"] if outcome != "allow" else ["TASK_AFTER"])
            assert [r.operation for r in s.task_state.after] == [operation]
            after = s.task_state.after[0]
            if operation in ("delete", "criterion_delete", "dependency_remove"):
                assert not hasattr(after, "saved")
            else:
                assert after.saved.id > 0
            assert after.user_id == s.user.id
        assert [r.operation for r in s.task_state.before] == [operation]

    @pytest.mark.parametrize("operation", OPERATIONS)
    async def test_after_failure_is_advisory_for_every_mutation(self, task_store, operation, caplog):
        s = task_store
        case = await prepare(s, operation)
        before = await case.snapshot()
        s.task_state.after_error = True
        result = await case.invoke()
        assert await case.snapshot() != before
        assert result.plugin_warnings[0].code == "TASK_PLUGIN_AFTER_SAVE_UNAVAILABLE"
        assert "SYNTHETIC_PRIVATE_TASK_ERROR" not in str(result.plugin_warnings)
        assert "SYNTHETIC_PRIVATE_TASK_ERROR" not in caplog.text

    async def test_missing_noop_and_foreign_data_do_not_dispatch(self, task_store):
        s = task_store
        case = await prepare(s, "update")
        unchanged = await s.tasks.update_task(s.user.id, case.task.id, TaskUpdate())
        assert unchanged.task.title == "Target" and unchanged.plugin_warnings == []
        assert (await s.tasks.update_task(s.user.id, -1, TaskUpdate(title="Missing"))).task is None
        assert not (await s.tasks.delete_task(s.user.id, -1)).success
        assert not (await s.tasks.delete_criterion(s.user.id, -1)).success
        assert not (await s.tasks.remove_dependency(s.user.id, -1, -2)).success
        with pytest.raises(NotFoundError):
            await s.tasks.update_criterion(s.user.id, -1, CriterionUpdate(met=True))
        foreign = uuid4()
        assert (await s.tasks.update_task(foreign, case.task.id, TaskUpdate(title="Foreign"))).task is None
        assert not (await s.tasks.delete_task(foreign, case.task.id)).success
        with pytest.raises(NotFoundError):
            await s.tasks.claim_task(foreign, case.task.id, "agent", 1)
        assert s.task_state.before == s.task_state.after == []

    async def test_inline_policy_rejection_precedes_first_task_write(self, task_store):
        s = task_store
        peer = await s.repos["task"].create_task(s.user.id, TaskCreate(plan_id=s.plan.id, title="Peer"))
        before_ids = [t.id for t in await s.repos["task"].list_tasks_for_user(s.user.id)]

        async def reject_inline(request):
            assert request.origin == "inline_create"
            assert not hasattr(request.parent_task, "id")
            assert request.target_task.id == peer.id
            return WriteDecision(outcome="reject", message="Inline blocked")

        s.tasks._task_plugin = TaskWritePluginService(TaskPlugin(before_save={"dependency_add": reject_inline}))
        with pytest.raises(TaskPluginRejectedError):
            await s.tasks.create_task(s.user.id, TaskCreate(
                plan_id=s.plan.id, title="Not saved", criteria=[CriterionCreate(description="Also not saved")],
                dependency_ids=[peer.id],
            ))
        assert [t.id for t in await s.repos["task"].list_tasks_for_user(s.user.id)] == before_ids
        assert (await s.repos["task"].get_task_by_id(s.user.id, peer.id)).criteria == []

    async def test_inline_confirmed_contexts_and_callback_order(self, task_store):
        s = task_store
        peer = await s.repos["task"].create_task(s.user.id, TaskCreate(plan_id=s.plan.id, title="Peer"))
        result = await s.tasks.create_task(s.user.id, TaskCreate(
            plan_id=s.plan.id, title="Inline", criteria=[CriterionCreate(description="Inline criterion")],
            dependency_ids=[peer.id],
        ))
        assert [r.operation for r in s.task_state.before] == ["create", "criterion_add", "dependency_add"]
        assert [r.operation for r in s.task_state.after] == ["create", "criterion_add", "dependency_add"]
        assert [r.origin for r in s.task_state.after] == ["direct", "inline_create", "inline_create"]
        assert s.task_state.after[1].saved.id == result.task.criteria[0].id
        assert s.task_state.after[2].saved.task_id == result.task.id
        assert result.task.dependency_ids == [peer.id]

    @pytest.mark.parametrize("registration", ["none", "empty", "update_only"])
    async def test_no_registered_create_preserves_partial_invalid_inline_behavior(self, task_store, registration):
        s = task_store
        if registration == "none":
            s.tasks._task_plugin = None
        else:
            async def allow(request):
                return WriteDecision(outcome="allow")
            s.tasks._task_plugin = TaskWritePluginService(TaskPlugin(
                before_save={"update": allow} if registration == "update_only" else {},
            ))
        before = len(await s.repos["task"].list_tasks_for_user(s.user.id))
        with pytest.raises(NotFoundError):
            await s.tasks.create_task(s.user.id, TaskCreate(
                plan_id=s.plan.id, title="Legacy partial row", dependency_ids=[-12345],
            ))
        assert len(await s.repos["task"].list_tasks_for_user(s.user.id)) == before + 1

    async def test_handler_cannot_rewrite_primary_or_nested_snapshots(self, task_store):
        s = task_store
        case = await prepare(s, "update")
        await s.repos["task"].create_criterion(s.user.id, case.task.id, CriterionCreate(description="Original criterion"))

        async def mutate_before(request):
            request.submitted.title = "Do not persist"
            request.previous.criteria[0].description = "Do not persist nested"
            request.proposed.title = "Do not persist proposal"
            request.plan.project_id = -1
            request.user_id = uuid4()
            return WriteDecision(outcome="allow")

        async def mutate_after(request):
            request.saved.title = "Do not change response"
            return None

        s.tasks._task_plugin = TaskWritePluginService(TaskPlugin(
            before_save={"update": mutate_before}, after_save={"update": mutate_after},
        ))
        result = await case.invoke()
        stored = await s.repos["task"].get_task_by_id(s.user.id, case.task.id)
        assert result.task.title == stored.title == "Updated"
        assert stored.criteria[0].description == "Original criterion"

    @pytest.mark.parametrize("mutation", ["criterion_added", "criterion_unmet", "version_changed"])
    async def test_paused_transition_rechecks_criteria_and_original_version(self, task_store, mutation):
        s = task_store
        case = await prepare(s, "transition")
        doing = await s.repos["task"].transition_task_state(s.user.id, case.task.id, TaskState.DOING, 1)
        criterion = None
        if mutation == "criterion_unmet":
            criterion = await s.repos["task"].create_criterion(s.user.id, doing.id, CriterionCreate(description="Initially met"))
            await s.repos["task"].update_criterion(s.user.id, criterion.id, CriterionUpdate(met=True))
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(request):
            entered.set()
            await release.wait()

        s.task_state.pause = pause
        work = s.spawn(s.tasks.transition_task(s.user.id, doing.id, TaskState.DONE, doing.version))
        await asyncio.wait_for(entered.wait(), 5)
        if mutation == "criterion_added":
            await s.repos["task"].create_criterion(s.user.id, doing.id, CriterionCreate(description="Added during policy"))
        elif mutation == "criterion_unmet":
            await s.repos["task"].update_criterion(s.user.id, criterion.id, CriterionUpdate(met=False))
        else:
            await s.repos["task"].transition_task_state(s.user.id, doing.id, TaskState.WAITING, doing.version)
        release.set()
        with pytest.raises(ConflictError if mutation == "version_changed" else InvalidStateTransitionError):
            await asyncio.wait_for(work, 5)
        saved = await s.repos["task"].get_task_by_id(s.user.id, doing.id)
        assert saved.state != TaskState.DONE and s.task_state.after == []

    @pytest.mark.parametrize("operation", ["claim", "transition"])
    async def test_paused_claim_or_doing_rechecks_dependencies(self, task_store, operation):
        s = task_store
        case = await prepare(s, operation)
        peer = await s.repos["task"].create_task(s.user.id, TaskCreate(plan_id=s.plan.id, title="Dependency"))
        peer = await s.repos["task"].transition_task_state(s.user.id, peer.id, TaskState.DOING, peer.version)
        peer = await s.repos["task"].transition_task_state(s.user.id, peer.id, TaskState.DONE, peer.version)
        await s.repos["task"].add_dependency(s.user.id, case.task.id, peer.id)
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(request):
            entered.set()
            await release.wait()

        s.task_state.pause = pause
        work = s.spawn(case.invoke())
        await asyncio.wait_for(entered.wait(), 5)
        await s.repos["task"].transition_task_state(s.user.id, peer.id, TaskState.TODO, peer.version)
        release.set()
        with pytest.raises(DependencyNotMetError):
            await asyncio.wait_for(work, 5)
        assert (await s.repos["task"].get_task_by_id(s.user.id, case.task.id)).state == TaskState.TODO
        assert s.task_state.after == []

    @pytest.mark.parametrize("status", [PlanStatus.COMPLETED, PlanStatus.ARCHIVED])
    async def test_create_rechecks_plan_after_policy(self, task_store, status):
        s = task_store
        case = await prepare(s, "create")
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(request):
            entered.set()
            await release.wait()

        s.task_state.pause = pause
        work = s.spawn(case.invoke())
        await asyncio.wait_for(entered.wait(), 5)
        await s.repos["plan"].update_plan(s.user.id, s.plan.id, PlanUpdate(status=status))
        release.set()
        with pytest.raises(InvalidStateTransitionError):
            await asyncio.wait_for(work, 5)
        assert await case.snapshot() == [] and s.task_state.after == []

    @pytest.mark.parametrize("operation", ["delete", "criterion_delete", "dependency_remove"])
    async def test_disappearance_during_policy_returns_false_without_after(self, task_store, operation):
        s = task_store
        case = await prepare(s, operation)

        async def remove(request):
            if operation == "delete":
                await s.repos["task"].delete_task(s.user.id, case.task.id)
            elif operation == "criterion_delete":
                await s.repos["task"].delete_criterion(s.user.id, case.criterion.id)
            else:
                await s.repos["task"].remove_dependency(s.user.id, case.task.id, case.peer.id)

        s.task_state.pause = remove
        assert not (await case.invoke()).success
        assert s.task_state.after == []

    async def test_task_after_failure_does_not_suppress_plan_completion(self, task_store):
        s = task_store
        task = await s.repos["task"].create_task(s.user.id, TaskCreate(plan_id=s.plan.id, title="Final task"))
        doing = await s.repos["task"].transition_task_state(s.user.id, task.id, TaskState.DOING, task.version)
        s.task_state.outcome = "warn"
        s.task_state.after_error = True
        result = await s.tasks.transition_task(s.user.id, task.id, TaskState.DONE, doing.version)
        assert [w.code for w in result.plugin_warnings] == [
            "TASK_TEST", "TASK_PLUGIN_AFTER_SAVE_UNAVAILABLE", "PLAN_AFTER",
        ]
        assert (await s.repos["plan"].get_plan_by_id(s.user.id, s.plan.id)).status == PlanStatus.COMPLETED
        assert (await s.repos["task"].get_task_by_id(s.user.id, task.id)).state == TaskState.DONE

    @pytest.mark.parametrize("cascade", ["task", "plan", "project"])
    async def test_cascade_callback_contract(self, task_store, cascade):
        s = task_store
        case = await prepare(s, "criterion_delete")
        if cascade == "task":
            assert (await s.tasks.delete_task(s.user.id, case.task.id)).success
            assert [x.operation for x in s.task_state.before] == ["delete"]
        elif cascade == "plan":
            assert (await s.plans.delete_plan(s.user.id, s.plan.id)).success
            assert s.task_state.before == []
        else:
            assert await s.repos["project"].delete_project(s.user.id, s.project.id)
            assert s.task_state.before == []
        assert await s.repos["task"].get_criterion_by_id(s.user.id, case.criterion.id) is None
        assert [x.operation for x in s.task_state.after] == (["delete"] if cascade == "task" else [])

    @pytest.mark.parametrize("operation", ["claim", "transition"])
    async def test_original_client_version_is_not_upgraded_after_handler(self, task_store, operation):
        s = task_store
        case = await prepare(s, operation)
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(request):
            entered.set()
            await release.wait()

        s.task_state.pause = pause
        work = s.spawn(case.invoke())
        await asyncio.wait_for(entered.wait(), 5)
        await s.repos["task"].transition_task_state(s.user.id, case.task.id, TaskState.WAITING, case.task.version)
        release.set()
        with pytest.raises(ConflictError):
            await asyncio.wait_for(work, 5)
        saved = await s.repos["task"].get_task_by_id(s.user.id, case.task.id)
        assert saved.version == case.task.version + 1 and saved.state == TaskState.WAITING
        assert s.task_state.after == []

    async def test_dependency_cycle_added_during_handler_is_rechecked(self, task_store):
        s = task_store
        case = await prepare(s, "dependency_add")
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(request):
            entered.set()
            await release.wait()

        s.task_state.pause = pause
        work = s.spawn(case.invoke())
        await asyncio.wait_for(entered.wait(), 5)
        await s.repos["task"].add_dependency(s.user.id, case.peer.id, case.task.id)
        release.set()
        with pytest.raises(CyclicDependencyError):
            await asyncio.wait_for(work, 5)
        assert (await s.repos["task"].get_task_by_id(s.user.id, case.task.id)).dependency_ids == []
        assert (await s.repos["task"].get_task_by_id(s.user.id, case.peer.id)).dependency_ids == [case.task.id]
        assert s.task_state.after == []

    async def test_criterion_add_rechecks_completed_parent_after_handler(self, task_store):
        s = task_store
        case = await prepare(s, "criterion_add")
        doing = await s.repos["task"].transition_task_state(s.user.id, case.task.id, TaskState.DOING, case.task.version)
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(request):
            entered.set()
            await release.wait()

        s.task_state.pause = pause
        work = s.spawn(case.invoke())
        await asyncio.wait_for(entered.wait(), 5)
        await s.repos["task"].transition_task_state(s.user.id, doing.id, TaskState.DONE, doing.version)
        release.set()
        with pytest.raises(InvalidStateTransitionError):
            await asyncio.wait_for(work, 5)
        assert (await s.repos["task"].get_task_by_id(s.user.id, doing.id)).criteria == []
        assert s.task_state.after == []

    @pytest.mark.parametrize("phase", ["before", "after"])
    async def test_real_request_cancellation_preserves_actual_commit_boundary(self, task_store, phase):
        s = task_store
        entered = asyncio.Event()

        async def wait(request):
            entered.set()
            await asyncio.Event().wait()

        registration = TaskPlugin(**{phase + "_save": {"create": wait}})
        s.tasks._task_plugin = TaskWritePluginService(registration)
        work = s.spawn(s.tasks.create_task(s.user.id, TaskCreate(plan_id=s.plan.id, title="Cancellation boundary")))
        await asyncio.wait_for(entered.wait(), 5)
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        tasks = await s.repos["task"].list_tasks_for_user(s.user.id)
        assert len(tasks) == (1 if phase == "after" else 0)

    @pytest.mark.parametrize("operation", ["criterion_update", "criterion_delete", "dependency_add", "dependency_remove"])
    async def test_foreign_criterion_and_dependency_paths_expose_nothing_to_handlers(self, task_store, operation):
        s = task_store
        case = await prepare(s, operation)
        before = await case.snapshot()
        foreign = uuid4()
        if operation == "criterion_update":
            with pytest.raises(NotFoundError):
                await s.tasks.update_criterion(foreign, case.criterion.id, CriterionUpdate(met=True))
        elif operation == "criterion_delete":
            assert not (await s.tasks.delete_criterion(foreign, case.criterion.id)).success
        elif operation == "dependency_add":
            with pytest.raises(NotFoundError):
                await s.tasks.add_dependency(foreign, case.task.id, case.peer.id)
        else:
            assert not (await s.tasks.remove_dependency(foreign, case.task.id, case.peer.id)).success
        assert await case.snapshot() == before
        assert s.task_state.before == s.task_state.after == []

    async def test_invalid_existing_state_is_checked_before_policy(self, task_store):
        s = task_store
        case = await prepare(s, "claim")
        doing = await s.repos["task"].transition_task_state(s.user.id, case.task.id, TaskState.DOING, 1)
        with pytest.raises(InvalidStateTransitionError):
            await s.tasks.claim_task(s.user.id, doing.id, "agent", doing.version)
        done = await s.repos["task"].transition_task_state(s.user.id, doing.id, TaskState.DONE, doing.version)
        with pytest.raises(InvalidStateTransitionError):
            await s.tasks.add_criterion(s.user.id, done.id, CriterionCreate(description="Not added"))
        assert s.task_state.before == s.task_state.after == []
