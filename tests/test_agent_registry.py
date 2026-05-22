import pytest
from src.agent.registry import AgentRegistry, AgentStatus


class TestAgentRegistry:
    def setup_method(self):
        self.registry = AgentRegistry()

    def test_register_agent(self):
        agent_id = self.registry.register("test-agent", "worker.processor")
        assert agent_id is not None
        assert self.registry.count() == 1

    def test_get_agent(self):
        agent_id = self.registry.register("test-agent", "worker.processor")
        agent = self.registry.get(agent_id)
        assert agent is not None
        assert agent["name"] == "test-agent"
        assert agent["type"] == "worker.processor"

    def test_get_nonexistent_agent(self):
        agent = self.registry.get("nonexistent-id")
        assert agent is None

    def test_list_agents(self):
        self.registry.register("agent-1", "worker.processor")
        self.registry.register("agent-2", "worker.analyzer")
        self.registry.register("agent-3", "monitor.watcher")
        assert len(self.registry.list()) == 3

    def test_list_agents_by_group(self):
        self.registry.register("agent-1", "worker.processor")
        self.registry.register("agent-2", "monitor.watcher")
        workers = self.registry.list(group="worker")
        assert len(workers) == 1

    def test_update_status(self):
        agent_id = self.registry.register("test-agent", "worker.processor")
        assert self.registry.update_status(agent_id, AgentStatus.RUNNING)
        agent = self.registry.get(agent_id)
        assert agent["status"] == "running"

    def test_refresh_worker_capabilities_on_reconnect(self):
        """Capabilities are refreshed on reconnect with a bumped epoch."""
        agent_id = self.registry.register(
            "test-agent",
            "worker.processor",
            capabilities=["summarize", "embed"],
        )

        snapshot = self.registry.refresh_capabilities(
            agent_id,
            ["search", "summarize"],
        )

        assert snapshot == {
            "id": agent_id,
            "status": "pending",
            "capabilities": ["search", "summarize"],
            "capability_epoch": 2,
        }
        agent = self.registry.get(agent_id)
        assert agent["capabilities"] == ["search", "summarize"]
        assert agent["audit"][-1] == {
            "event": "worker_capabilities_refreshed",
            "capability_epoch": 2,
            "capability_count": 2,
        }

    def test_register_with_capabilities_stores_epoch_and_audit(self):
        """Registration with capabilities initialises epoch and audit."""
        agent_id = self.registry.register(
            "capable-worker",
            "worker.processor",
            capabilities=["  transcribe ", "embed", "", "TRANSCRIBE"],
        )
        agent = self.registry.get(agent_id)
        assert agent["capabilities"] == ["embed", "transcribe"]
        assert agent["capability_epoch"] == 1
        assert agent["audit"][0] == {
            "event": "worker_registered",
            "capability_epoch": 1,
            "capability_count": 2,
        }

    def test_worker_snapshot_returns_claims_safe_view(self):
        """Worker snapshot returns only the fields needed for claims."""
        agent_id = self.registry.register(
            "snap-worker",
            "worker.processor",
            capabilities=["run"],
        )
        snapshot = self.registry.worker_snapshot(agent_id)
        assert snapshot == {
            "id": agent_id,
            "status": "pending",
            "capabilities": ["run"],
            "capability_epoch": 1,
        }

    def test_worker_snapshot_returns_none_for_unknown_agent(self):
        assert self.registry.worker_snapshot("missing") is None

    def test_refresh_capabilities_returns_none_for_unknown_agent(self):
        assert self.registry.refresh_capabilities("missing", ["run"]) is None

    def test_delete_agent(self):
        agent_id = self.registry.register("test-agent", "worker.processor")
        assert self.registry.delete(agent_id)
        assert self.registry.count() == 0

    def test_delete_nonexistent_agent(self):
        assert not self.registry.delete("nonexistent-id")

# 2019-01-23T10:28:57 update

# 2019-01-28T18:15:57 update

# 2019-02-22T11:46:37 update

# 2019-03-27T14:43:52 update

# 2019-04-12T16:58:25 update

# 2019-05-27T15:15:18 update

# 2019-07-17T14:36:58 update

# 2019-09-06T12:29:31 update

# 2019-11-27T17:43:26 update

# 2019-11-28T08:42:43 update

# 2019-12-03T20:34:02 update

# 2019-12-26T08:15:09 update

# 2020-01-07T09:36:32 update

# 2020-01-10T12:44:52 update

# 2020-07-05T19:33:32 update

# 2020-07-07T14:16:11 update

# 2020-07-28T08:29:39 update

# 2020-08-26T18:58:21 update

# 2020-08-28T09:50:37 update

# 2020-09-17T15:23:33 update

# 2020-09-23T16:22:24 update

# 2020-10-14T13:27:24 update

# 2020-11-20T11:40:04 update

# 2020-12-10T13:55:01 update

# 2020-12-25T20:33:02 update

# 2021-03-22T19:53:48 update

# 2021-03-26T15:02:19 update

# 2021-07-16T20:24:40 update

# 2021-07-22T13:19:23 update

# 2021-08-16T19:11:26 update

# 2021-10-02T13:32:20 update

# 2021-10-23T18:31:31 update

# 2021-10-29T13:55:10 update

# 2022-07-31T17:35:39 update

# 2022-09-27T09:32:34 update

# 2022-11-07T14:44:52 update

# 2023-01-23T14:07:09 update

# 2023-03-16T15:23:38 update

# 2023-07-03T18:33:44 update

# 2023-07-27T09:35:11 update

# 2023-11-16T11:22:59 update

# 2023-12-20T14:25:29 update

# 2024-03-07T17:32:49 update

# 2024-04-10T10:50:42 update

# 2024-06-19T19:57:49 update

# 2024-12-05T18:02:46 update

# 2025-01-15T16:13:24 update

# 2025-03-12T20:58:57 update

# 2025-06-24T20:33:23 update

# 2025-08-25T10:56:35 update

# 2025-09-12T17:09:51 update

# 2025-10-06T20:01:10 update

# 2025-10-14T11:48:40 update

# 2026-01-29T13:09:29 update
