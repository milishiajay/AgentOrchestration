import pytest
from src.agent.registry import AgentRegistry, AgentStatus, _DISABLED_STATUSES


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

    def test_delete_agent(self):
        agent_id = self.registry.register("test-agent", "worker.processor")
        assert self.registry.delete(agent_id)
        assert self.registry.count() == 0

    def test_delete_nonexistent_agent(self):
        assert not self.registry.delete("nonexistent-id")

    # --- Issue #545: disabled entries must not leak in default listings ---

    def test_list_excludes_disabled_by_default(self):
        """Default list() should not return stopped/failed/terminated agents."""
        a1 = self.registry.register("alive", "worker.proc")
        a2 = self.registry.register("dead", "worker.proc")
        a3 = self.registry.register("crashed", "worker.proc")
        a4 = self.registry.register("gone", "worker.proc")

        self.registry.update_status(a2, AgentStatus.STOPPED)
        self.registry.update_status(a3, AgentStatus.FAILED)
        self.registry.update_status(a4, AgentStatus.TERMINATED)

        agents = self.registry.list()
        assert len(agents) == 1
        assert agents[0]["id"] == a1

    def test_list_include_disabled_true_returns_all(self):
        """list(include_disabled=True) should return all agents including disabled."""
        a1 = self.registry.register("alive", "worker.proc")
        a2 = self.registry.register("dead", "worker.proc")

        self.registry.update_status(a2, AgentStatus.STOPPED)

        agents = self.registry.list(include_disabled=True)
        assert len(agents) == 2

    def test_list_with_explicit_disabled_status(self):
        """When a specific disabled status is requested, it should be returned
        even without include_disabled."""
        a1 = self.registry.register("alive", "worker.proc")
        a2 = self.registry.register("dead", "worker.proc")
        self.registry.update_status(a2, AgentStatus.TERMINATED)

        agents = self.registry.list(status=AgentStatus.TERMINATED)
        assert len(agents) == 1
        assert agents[0]["id"] == a2

    def test_count_excludes_disabled_by_default(self):
        """Default count() should exclude disabled agents."""
        a1 = self.registry.register("alive", "worker.proc")
        a2 = self.registry.register("stopped", "worker.proc")
        self.registry.update_status(a2, AgentStatus.STOPPED)

        assert self.registry.count() == 1

    def test_count_include_disabled_true(self):
        """count(include_disabled=True) should include all."""
        a1 = self.registry.register("alive", "worker.proc")
        a2 = self.registry.register("stopped", "worker.proc")
        self.registry.update_status(a2, AgentStatus.STOPPED)

        assert self.registry.count(include_disabled=True) == 2

    def test_group_index_cleaned_on_disable(self):
        """When an agent moves to a disabled state, it should be removed
        from the group index so group queries don't leak it."""
        a1 = self.registry.register("alive", "worker.proc")
        a2 = self.registry.register("dead", "worker.proc")
        self.registry.update_status(a2, AgentStatus.TERMINATED)

        workers = self.registry.list(group="worker")
        assert len(workers) == 1
        assert workers[0]["id"] == a1

    def test_group_query_with_include_disabled(self):
        """Group query with include_disabled should return disabled members."""
        a1 = self.registry.register("alive", "worker.proc")
        a2 = self.registry.register("dead", "worker.proc")
        self.registry.update_status(a2, AgentStatus.TERMINATED)

        # With include_disabled, it searches all agents (not just index)
        workers = self.registry.list(group="worker", include_disabled=True)
        assert len(workers) == 2
        ids = {a["id"] for a in workers}
        assert ids == {a1, a2}

    def test_transition_from_disabled_to_active_restores_to_index(self):
        """Moving an agent from disabled back to active should be possible
        and it should reappear in default listings."""
        agent_id = self.registry.register("zombie", "worker.proc")
        self.registry.update_status(agent_id, AgentStatus.TERMINATED)
        assert self.registry.count() == 0
        assert len(self.registry.list(group="worker")) == 0

        # Bring it back
        self.registry.update_status(agent_id, AgentStatus.RUNNING)
        assert self.registry.count() == 1
        assert len(self.registry.list(group="worker")) == 1

    def test_all_disabled_statuses_filtered(self):
        """Verify STOPPED, FAILED, and TERMINATED are all filtered by default."""
        for status in _DISABLED_STATUSES:
            reg = AgentRegistry()
            reg.register("alive", "worker.proc")
            a2 = reg.register("disabled", "worker.proc")
            reg.update_status(a2, AgentStatus(status))
            assert reg.count() == 1, f"{status} should be excluded from count"
            assert len(reg.list()) == 1, f"{status} should be excluded from list"

    def test_pending_and_running_not_disabled(self):
        """PENDING, RUNNING, and PAUSED should still appear in default listings."""
        for status in [AgentStatus.PENDING, AgentStatus.RUNNING, AgentStatus.PAUSED]:
            reg = AgentRegistry()
            agent_id = reg.register("agent", "worker.proc")
            reg.update_status(agent_id, status)
            assert reg.count() == 1, f"{status} should be included in count"
            assert len(reg.list()) == 1, f"{status} should be included in list"
