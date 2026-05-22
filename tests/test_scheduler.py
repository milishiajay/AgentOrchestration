import asyncio

import pytest
from src.agent.registry import AgentRegistry
from src.orchestrator.scheduler import TaskScheduler


class TestTaskScheduler:
    def setup_method(self):
        self.scheduler = TaskScheduler()

    def test_enqueue_task(self):
        task_id = self.scheduler.enqueue({"type": "test", "payload": {}})
        assert task_id is not None

    def test_dequeue_task(self):
        self.scheduler.enqueue({"type": "test", "payload": {"data": 1}})
        task = asyncio.run(self.scheduler.dequeue())
        assert task is not None
        assert task["type"] == "test"

    def test_enqueue_multiple_priorities(self):
        self.scheduler.enqueue({"type": "low"}, priority=1)
        self.scheduler.enqueue({"type": "high"}, priority=10)
        task = asyncio.run(self.scheduler.dequeue())
        assert task["type"] == "high"

    def test_complete_task(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.complete(task["id"])

    def test_fail_task_with_retry(self):
        self.scheduler.enqueue({"type": "test"})
        task = asyncio.run(self.scheduler.dequeue())
        assert self.scheduler.fail(task["id"])

    # ──────────────────────────────────────────────
    #  Worker claim tests
    # ──────────────────────────────────────────────

    def test_worker_claim_defers_stale_epoch_after_reconnect(self):
        """A worker that reconnects with refreshed capabilities cannot
        claim a task pinned to its old epoch."""
        registry = AgentRegistry()
        agent_id = registry.register(
            "hot-reload-worker",
            "worker.processor",
            capabilities=["transcribe"],
        )
        stale_snapshot = registry.worker_snapshot(agent_id)
        current_snapshot = registry.refresh_capabilities(
            agent_id,
            ["summarize"],
        )
        task_id = self.scheduler.enqueue({
            "type": "summarize",
            "target_agent": agent_id,
            "required_capability": "transcribe",
            "worker_capability_epoch": stale_snapshot["capability_epoch"],
        })

        task = asyncio.run(
            self.scheduler.claim_for_worker(current_snapshot)
        )

        assert task is None
        assert task_id not in self.scheduler._in_flight
        assert self.scheduler.claim_audit()[-1] == {
            "event": "worker_claim_deferred",
            "task_id": task_id,
            "worker_id": agent_id,
            "worker_capability_epoch": current_snapshot["capability_epoch"],
            "reason": "stale_capability_epoch",
        }
        # Task is still in the queue
        deferred = asyncio.run(self.scheduler.dequeue_unvalidated())
        assert deferred["id"] == task_id

    def test_worker_claim_uses_refreshed_capabilities(self):
        """After reconnect, a worker should claim tasks matching its
        new capability set."""
        registry = AgentRegistry()
        agent_id = registry.register("hot-reload-worker", "worker.processor")
        current_snapshot = registry.refresh_capabilities(
            agent_id,
            ["summarize"],
        )
        task_id = self.scheduler.enqueue({
            "type": "summarize",
            "target_agent": agent_id,
            "required_capability": "summarize",
        })

        task = asyncio.run(
            self.scheduler.claim_for_worker(current_snapshot)
        )

        assert task["id"] == task_id
        assert task["claimed_by"] == agent_id
        assert (
            task["worker_capability_epoch"]
            == current_snapshot["capability_epoch"]
        )
        assert task_id in self.scheduler._in_flight

    def test_worker_complete_rejects_stale_epoch_after_reconnect(self):
        """Completing a task with a stale worker epoch requeues the task."""
        registry = AgentRegistry()
        agent_id = registry.register(
            "hot-reload-worker",
            "worker.processor",
            capabilities=["summarize"],
        )
        claimed_snapshot = registry.worker_snapshot(agent_id)
        task_id = self.scheduler.enqueue({
            "type": "summarize",
            "target_agent": agent_id,
            "required_capability": "summarize",
        })
        task = asyncio.run(
            self.scheduler.claim_for_worker(claimed_snapshot)
        )
        current_snapshot = registry.refresh_capabilities(
            agent_id,
            ["translate"],
        )

        assert not self.scheduler.complete_for_worker(
            current_snapshot,
            task["id"],
        )

        assert task_id not in self.scheduler._in_flight
        assert self.scheduler.claim_audit()[-1] == {
            "event": "worker_ack_rejected",
            "task_id": task_id,
            "worker_id": agent_id,
            "worker_capability_epoch": current_snapshot["capability_epoch"],
            "action": "complete",
            "reason": "stale_capability_epoch",
        }
        # Task was requeued
        deferred = asyncio.run(self.scheduler.dequeue_unvalidated())
        assert deferred["id"] == task_id
        assert deferred["retries"] == 0  # complete doesn't increment retries

    def test_worker_fail_rejects_wrong_worker_without_losing_task(self):
        """A different worker cannot ack a task claimed by another worker."""
        registry = AgentRegistry()
        agent_id = registry.register("primary-worker", "worker.processor")
        other_agent_id = registry.register(
            "other-worker",
            "worker.processor",
        )
        task_id = self.scheduler.enqueue({
            "type": "summarize",
            "target_agent": agent_id,
        })
        task = asyncio.run(
            self.scheduler.claim_for_worker(
                registry.worker_snapshot(agent_id),
            )
        )

        assert not self.scheduler.fail_for_worker(
            registry.worker_snapshot(other_agent_id),
            task["id"],
        )

        assert task_id in self.scheduler._in_flight  # task stays in-flight
        assert self.scheduler.claim_audit()[-1] == {
            "event": "worker_ack_rejected",
            "task_id": task_id,
            "worker_id": other_agent_id,
            "worker_capability_epoch": 1,
            "action": "fail",
            "reason": "worker_mismatch",
        }

    def test_worker_claim_defers_missing_capability(self):
        """A task requiring a capability the worker doesn't have is deferred."""
        registry = AgentRegistry()
        agent_id = registry.register(
            "worker",
            "worker.processor",
            capabilities=["basic"],
        )
        snapshot = registry.worker_snapshot(agent_id)
        task_id = self.scheduler.enqueue({
            "type": "special",
            "target_agent": agent_id,
            "required_capability": "advanced",
        })

        task = asyncio.run(self.scheduler.claim_for_worker(snapshot))

        assert task is None
        assert self.scheduler.claim_audit()[-1]["reason"] == "missing_capability"
        deferred = asyncio.run(self.scheduler.dequeue_unvalidated())
        assert deferred["id"] == task_id

    def test_worker_fail_stale_epoch_requeues_with_retry(self):
        """Failing a task with stale epoch requeues and counts retries."""
        self.scheduler._max_retries = 5
        registry = AgentRegistry()
        agent_id = registry.register(
            "worker",
            "worker.processor",
            capabilities=["run"],
        )
        old_snapshot = registry.worker_snapshot(agent_id)
        task_id = self.scheduler.enqueue({
            "type": "task",
            "target_agent": agent_id,
        })
        task = asyncio.run(self.scheduler.claim_for_worker(old_snapshot))

        new_snapshot = registry.refresh_capabilities(agent_id, ["run"])
        assert not self.scheduler.fail_for_worker(new_snapshot, task["id"])

        deferred = asyncio.run(self.scheduler.dequeue_unvalidated())
        assert deferred["id"] == task_id
        assert deferred["retries"] == 1  # fail increments retries before requeue

    def test_retry_requeue_preserves_task_id(self):
        """When a stale task is requeued, it keeps its original ID."""
        registry = AgentRegistry()
        agent_id = registry.register("worker", "worker.processor", capabilities=["run"])
        old_snap = registry.worker_snapshot(agent_id)
        task_id = self.scheduler.enqueue({
            "type": "task",
            "target_agent": agent_id,
        })
        task = asyncio.run(self.scheduler.claim_for_worker(old_snap))
        new_snap = registry.refresh_capabilities(agent_id, ["run"])
        self.scheduler.fail_for_worker(new_snap, task["id"])
        deferred = asyncio.run(self.scheduler.dequeue_unvalidated())
        assert deferred["id"] == task_id

    def test_claim_worker_target_agent_mismatch(self):
        """A worker cannot claim a task meant for a different agent."""
        registry = AgentRegistry()
        a1 = registry.register("agent-1", "worker.processor", capabilities=["run"])
        a2 = registry.register("agent-2", "worker.processor", capabilities=["run"])
        self.scheduler.enqueue({
            "type": "task",
            "target_agent": a1,
        })
        task = asyncio.run(
            self.scheduler.claim_for_worker(registry.worker_snapshot(a2))
        )
        assert task is None
        assert self.scheduler.claim_audit()[-1]["reason"] == "target_agent_mismatch"

    def test_claim_accepts_task_without_target_agent(self):
        """A task without a target_agent field is claimable by any worker."""
        registry = AgentRegistry()
        agent_id = registry.register("worker", "worker.processor", capabilities=["run"])
        self.scheduler.enqueue({"type": "generic"})
        task = asyncio.run(
            self.scheduler.claim_for_worker(registry.worker_snapshot(agent_id))
        )
        assert task is not None
        assert task["claimed_by"] == agent_id

# 2019-01-09T19:07:03 update

# 2019-02-18T12:30:02 update

# 2019-04-11T16:04:51 update

# 2019-04-17T16:25:46 update

# 2019-05-24T19:32:13 update

# 2019-07-02T12:54:25 update

# 2019-07-03T20:37:00 update

# 2019-08-21T19:37:17 update

# 2019-10-18T10:30:31 update

# 2019-10-25T09:01:38 update

# 2019-10-29T12:59:34 update

# 2019-11-05T10:07:06 update

# 2019-11-11T10:43:52 update

# 2020-01-17T13:40:02 update

# 2020-02-07T14:06:34 update

# 2020-04-03T08:53:40 update

# 2020-04-06T19:36:29 update

# 2020-05-12T11:51:05 update

# 2020-08-17T08:37:15 update

# 2020-09-15T10:39:38 update

# 2020-10-06T11:26:19 update

# 2020-10-21T13:32:43 update

# 2020-12-14T18:18:36 update

# 2020-12-23T17:15:03 update

# 2021-01-25T16:29:00 update

# 2021-02-23T11:23:50 update

# 2021-03-19T12:21:19 update

# 2021-07-29T18:48:25 update

# 2021-08-25T12:46:58 update

# 2021-09-09T16:27:13 update

# 2021-12-16T12:05:30 update

# 2022-05-07T14:05:12 update

# 2022-07-18T20:52:29 update

# 2022-07-31T18:42:26 update

# 2022-09-09T13:10:08 update

# 2023-01-04T15:16:57 update

# 2023-01-17T14:49:04 update

# 2023-02-15T13:51:30 update

# 2023-03-08T09:15:53 update

# 2023-03-23T16:32:20 update

# 2023-03-28T09:32:01 update

# 2023-05-05T17:28:22 update

# 2023-06-01T08:13:52 update

# 2023-06-20T09:58:10 update

# 2023-07-04T16:14:34 update

# 2023-07-17T20:49:40 update

# 2023-12-26T11:49:18 update

# 2024-05-27T11:00:06 update

# 2024-07-04T08:53:03 update

# 2024-07-18T16:19:02 update

# 2024-08-07T09:35:35 update

# 2024-08-22T14:32:14 update

# 2025-05-20T14:19:23 update

# 2025-07-17T17:54:48 update

# 2025-07-28T13:06:30 update

# 2025-12-22T19:05:25 update

# 2026-01-08T18:43:02 update

# 2026-01-12T16:53:28 update

# 2026-04-16T16:58:23 update
