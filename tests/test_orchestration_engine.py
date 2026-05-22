import asyncio

import pytest

from src.orchestrator.engine import OrchestrationEngine


class TestOrchestrationEngine:
    def setup_method(self):
        self.engine = OrchestrationEngine()

    def test_enqueue_task_pins_worker_capability_epoch(self):
        """Enqueuing a task pins it to the worker's current capability epoch."""
        agent_id = self.engine.registry.register(
            "hot-reload-worker",
            "worker.processor",
            capabilities=["summarize"],
        )

        task_id = self.engine.enqueue_task(
            agent_id,
            {"type": "summarize"},
            required_capability="summarize",
        )
        task = asyncio.run(self.engine.scheduler.dequeue())

        assert task["id"] == task_id
        assert task["target_agent"] == agent_id
        assert task["required_capability"] == "summarize"
        assert task["worker_capability_epoch"] == 1

    def test_dequeue_defers_stale_worker_epoch_after_reconnect(self):
        """Dequeue defers a task when the worker reconnected with new capabilities."""
        agent_id = self.engine.registry.register(
            "hot-reload-worker",
            "worker.processor",
            capabilities=["summarize"],
        )
        task_id = self.engine.enqueue_task(
            agent_id,
            {"type": "summarize"},
            required_capability="summarize",
        )
        # Worker hot-reloads with different capabilities
        self.engine.registry.refresh_capabilities(agent_id, ["translate"])

        task = asyncio.run(self.engine.scheduler.dequeue())

        assert task is None
        assert task_id not in self.engine.scheduler._in_flight
        assert self.engine.dispatch_decisions[-1] == {
            "task_id": task_id,
            "target_agent": agent_id,
            "allowed": False,
            "reason": "stale_capability_epoch",
        }
        # Audit trail is recorded in scheduler
        assert self.engine.scheduler.claim_audit()[-1] == {
            "event": "task_dispatch_deferred",
            "task_id": task_id,
            "target_agent": agent_id,
            "reason": "stale_capability_epoch",
        }

    def test_enqueue_task_rejects_unknown_agent(self):
        """Enqueuing to a non-existent agent raises an error."""
        with pytest.raises(ValueError, match="Agent missing not found"):
            self.engine.enqueue_task("missing", {"type": "summarize"})

    def test_dequeue_defers_missing_capability_before_execution(self):
        """A task requiring a capability the worker lacks is deferred."""
        agent_id = self.engine.registry.register(
            "worker",
            "worker.processor",
            capabilities=["basic"],
        )
        task_id = self.engine.enqueue_task(
            agent_id,
            {"type": "advanced"},
            required_capability="advanced",
        )

        task = asyncio.run(self.engine.scheduler.dequeue())

        assert task is None
        assert self.engine.dispatch_decisions[-1]["reason"] == "missing_capability"

    def test_reconnect_refresh_preserves_worker_identity(self):
        """After reconnect, the agent ID stays the same, only epoch changes."""
        agent_id = self.engine.registry.register(
            "worker",
            "worker.processor",
            capabilities=["v1"],
        )
        self.engine.registry.refresh_capabilities(agent_id, ["v1", "v2"])

        agent = self.engine.registry.get(agent_id)
        assert agent["id"] == agent_id
        assert agent["capability_epoch"] == 2
        assert agent["capabilities"] == ["v1", "v2"]
        assert agent["audit"][-1]["event"] == "worker_capabilities_refreshed"

    def test_capability_survives_disconnect_reconnect_cycle(self):
        """Full cycle: register -> enqueue -> reconnect -> dequeue defers -> no data loss."""
        agent_id = self.engine.registry.register(
            "worker",
            "worker.processor",
            capabilities=["summarize"],
        )
        # Enqueue several tasks
        ids = []
        for i in range(3):
            ids.append(self.engine.enqueue_task(
                agent_id,
                {"type": f"task-{i}"},
                required_capability="summarize",
            ))

        # Worker reconnects with refreshed capabilities
        self.engine.registry.refresh_capabilities(agent_id, ["translate"])

        # All tasks should be deferred (stale epoch)
        for _ in range(3):
            task = asyncio.run(self.engine.scheduler.dequeue())
            assert task is None

        # Tasks are still in queue (not lost)
        assert len(self.engine.scheduler._queues["default"]) == 3

        # Each dequeue examines all 3 tasks, so 3*3=9 audit decisions
        audit = self.engine.scheduler.claim_audit()
        assert len(audit) == 9

        # All decisions should be stale_capability_epoch
        for audit_entry in audit:
            assert audit_entry["event"] == "task_dispatch_deferred"
            assert audit_entry["reason"] == "stale_capability_epoch"

    def test_unknown_agent_fails_closed(self):
        """Dequeue for an unknown agent rejects safely (no crash)."""
        # Create a task with a target that doesn't exist
        self.engine.scheduler.enqueue({
            "type": "orphan",
            "target_agent": "nonexistent",
        })
        task = asyncio.run(self.engine.scheduler.dequeue())
        # Should be deferred because agent doesn't exist
        assert task is None
        assert self.engine.scheduler.claim_audit()[-1] == {
            "event": "task_dispatch_deferred",
            "task_id": self.engine.scheduler.claim_audit()[-1]["task_id"],
            "target_agent": "nonexistent",
            "reason": "agent_not_found",
        }
