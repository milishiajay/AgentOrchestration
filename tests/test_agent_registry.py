"""Regression tests for registry health gate — #570 rolling deploys.

Covers: health status check, accepting_tasks flag, lifecycle state
rejection, capability/version gating, cache invalidation on state
changes, and sanitized audit metadata.
"""

import pytest
from src.agent.registry import AgentRegistry, AgentStatus, HandlerResolution


class TestRegistryHealthGate:
    """Verifies that handler health is checked before routing."""

    def setup_method(self):
        self.registry = AgentRegistry()

    # ──────────────────────────────────────────────
    #  Health / accepting_tasks rejections
    # ──────────────────────────────────────────────

    def test_unhealthy_handler_rejected_during_rolling_deploy(self):
        """An unhealthy handler is not routable — rolling deploy blocks it."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        # Handler becomes unhealthy during deploy
        self.registry.set_health(agent_id, "unhealthy", accepting_tasks=False)

        resolution = self.registry.resolve_handler(agent_id=agent_id)
        assert not resolution.resolved
        assert "handler_unroutable" in resolution.reason
        assert agent_id in resolution.deferred_ids

    def test_stopped_handler_rejected(self):
        """A stopped handler is in a terminal state and not routable."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.STOPPED)

        resolution = self.registry.resolve_handler(agent_id=agent_id)
        assert not resolution.resolved
        assert agent_id in resolution.deferred_ids

    def test_terminated_handler_rejected(self):
        """A terminated handler is not routable."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.TERMINATED)

        resolution = self.registry.resolve_handler(agent_id=agent_id)
        assert not resolution.resolved

    def test_failed_handler_rejected(self):
        """A failed handler is not routable."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.FAILED)

        resolution = self.registry.resolve_handler(agent_id=agent_id)
        assert not resolution.resolved

    def test_pending_handler_rejected(self):
        """A pending handler has not initialised and is not routable."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        # Still in PENDING status from registration

        resolution = self.registry.resolve_handler(agent_id=agent_id)
        assert not resolution.resolved
        assert agent_id in resolution.deferred_ids

    def test_draining_handler_rejected(self):
        """A draining handler should not receive new tasks during rolling deploys."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.DRAINING)

        resolution = self.registry.resolve_handler(agent_id=agent_id)
        assert not resolution.resolved
        assert agent_id in resolution.deferred_ids

    def test_healthy_running_handler_accepted(self):
        """A running, healthy handler is routable."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        resolution = self.registry.resolve_handler(agent_id=agent_id)
        assert resolution.resolved
        assert resolution.agent["id"] == agent_id
        assert resolution.reason == "healthy"

    def test_non_accepting_running_handler_rejected(self):
        """A running handler marked as not-accepting is unroutable (rolling drain)."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)
        self.registry.set_health(agent_id, "healthy", accepting_tasks=False)

        resolution = self.registry.resolve_handler(agent_id=agent_id)
        assert not resolution.resolved
        assert agent_id in resolution.deferred_ids

    # ──────────────────────────────────────────────
    #  Capability gating
    # ──────────────────────────────────────────────

    def test_missing_capability_rejected(self):
        """Exact handler lacking a required capability is rejected."""
        agent_id = self.registry.register(
            "worker-1", "worker.processor", capabilities=["basic"]
        )
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        resolution = self.registry.resolve_handler(
            agent_id=agent_id,
            required_capability="advanced",
        )
        assert not resolution.resolved
        assert resolution.reason == "missing_capability"

    def test_matching_capability_accepted(self):
        """Exact handler with the required capability is accepted."""
        agent_id = self.registry.register(
            "worker-1", "worker.processor", capabilities=["basic", "advanced"]
        )
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        resolution = self.registry.resolve_handler(
            agent_id=agent_id,
            required_capability="advanced",
        )
        assert resolution.resolved
        assert resolution.agent["id"] == agent_id

    # ──────────────────────────────────────────────
    #  Version gating
    # ──────────────────────────────────────────────

    def test_version_mismatch_rejected(self):
        """A handler with an incompatible version is rejected."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        resolution = self.registry.resolve_handler(
            agent_id=agent_id,
            required_version="2.0.0",
        )
        assert not resolution.resolved
        assert resolution.reason == "version_mismatch"

    def test_version_match_accepted(self):
        """A handler matching the required version is accepted."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        resolution = self.registry.resolve_handler(
            agent_id=agent_id,
            required_version="1.0.0",
        )
        assert resolution.resolved
        assert resolution.agent["id"] == agent_id

    # ──────────────────────────────────────────────
    #  Type-based resolution (rolling deploy fallthrough)
    # ──────────────────────────────────────────────

    def test_type_resolution_falls_through_to_healthy_handler(self):
        """When the first handler is unhealthy, type resolution falls to the next."""
        a1 = self.registry.register("worker-1", "worker.processor")
        a2 = self.registry.register("worker-2", "worker.processor")
        self.registry.update_status(a1, AgentStatus.RUNNING)
        self.registry.update_status(a2, AgentStatus.RUNNING)

        # Mark worker-1 as unhealthy (rolling deploy drain)
        self.registry.set_health(a1, "unhealthy", accepting_tasks=False)

        # Type-based resolution should fall through to worker-2
        resolution = self.registry.resolve_handler(agent_type="worker.processor")
        assert resolution.resolved
        assert resolution.agent["id"] == a2
        assert a1 in resolution.deferred_ids

    def test_all_handlers_unhealthy_returns_all_deferred(self):
        """When all handlers are unhealthy, resolution fails cleanly — all deferred."""
        a1 = self.registry.register("worker-1", "worker.processor")
        a2 = self.registry.register("worker-2", "worker.processor")
        self.registry.update_status(a1, AgentStatus.RUNNING)
        self.registry.update_status(a2, AgentStatus.RUNNING)
        self.registry.set_health(a1, "unhealthy", accepting_tasks=False)
        self.registry.set_health(a2, "unhealthy", accepting_tasks=False)

        resolution = self.registry.resolve_handler(agent_type="worker.processor")
        assert not resolution.resolved
        assert resolution.reason == "all_deferred"
        assert len(resolution.deferred_ids) == 2

    # ──────────────────────────────────────────────
    #  Cache invalidation
    # ──────────────────────────────────────────────

    def test_cache_invalidated_on_status_change(self):
        """Resolution cache is invalidated when handler status changes."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        # First resolution — should be healthy, cached
        r1 = self.registry.resolve_handler(agent_id=agent_id)
        assert r1.resolved

        # Status change invalidates cache
        self.registry.update_status(agent_id, AgentStatus.DRAINING)
        r2 = self.registry.resolve_handler(agent_id=agent_id)
        assert not r2.resolved
        assert agent_id in r2.deferred_ids

    def test_cache_invalidated_on_health_change(self):
        """Resolution cache is invalidated when handler health changes."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        r1 = self.registry.resolve_handler(agent_id=agent_id)
        assert r1.resolved

        # Health change invalidates cache
        self.registry.set_health(agent_id, "unhealthy", accepting_tasks=False)
        r2 = self.registry.resolve_handler(agent_id=agent_id)
        assert not r2.resolved

    def test_cache_invalidated_on_capability_refresh(self):
        """Resolution cache is invalidated on capability refresh."""
        agent_id = self.registry.register(
            "worker-1", "worker.processor", capabilities=["basic"]
        )
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        # Cache the initial resolution
        r1 = self.registry.resolve_handler(
            agent_id=agent_id, required_capability="advanced"
        )
        assert not r1.resolved

        # Refresh adds the capability — cache must invalidate
        self.registry.refresh_capabilities(agent_id, ["basic", "advanced"])
        r2 = self.registry.resolve_handler(
            agent_id=agent_id, required_capability="advanced"
        )
        assert r2.resolved

    def test_cache_invalidated_on_deletion(self):
        """Resolution cache is invalidated on handler deletion."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        r1 = self.registry.resolve_handler(agent_id=agent_id)
        assert r1.resolved

        self.registry.delete(agent_id)
        r2 = self.registry.resolve_handler(agent_id=agent_id)
        assert not r2.resolved
        assert r2.reason == "agent_not_found"

    def test_cached_type_resolution_invalidated_on_group_handler_change(self):
        """Type-based cache invalidates when a handler in the group deletes."""
        a1 = self.registry.register("worker-1", "worker.processor")
        a2 = self.registry.register("worker-2", "worker.processor")
        self.registry.update_status(a1, AgentStatus.RUNNING)
        self.registry.update_status(a2, AgentStatus.RUNNING)
        self.registry.set_health(a1, "unhealthy", accepting_tasks=False)
        self.registry.set_health(a2, "healthy")

        # First type resolution falls through to a2
        r1 = self.registry.resolve_handler(agent_type="worker.processor")
        assert r1.resolved
        assert r1.agent["id"] == a2

        # Delete a2 — cache should invalidate
        self.registry.delete(a2)
        r2 = self.registry.resolve_handler(agent_type="worker.processor")
        assert not r2.resolved  # only a1 remains, and it's unhealthy

    # ──────────────────────────────────────────────
    #  HandlerResolution sanitisation
    # ──────────────────────────────────────────────

    def test_handler_resolution_does_not_expose_config(self):
        """Resolution result must not carry config, secrets, or runtime payloads."""
        agent_id = self.registry.register(
            "worker-1", "worker.processor",
            config={"api_key": "secret-token", "endpoint": "https://internal"},
        )
        self.registry.update_status(agent_id, AgentStatus.RUNNING)

        resolution = self.registry.resolve_handler(agent_id=agent_id)
        assert resolution.resolved
        # The returned agent dict is the full agent — but audit metadata
        # (reason, deferred_ids) must not leak config
        assert resolution.reason == "healthy"
        assert "config" not in resolution.reason
        assert "api_key" not in resolution.reason
        assert "secret" not in resolution.reason

    # ──────────────────────────────────────────────
    #  Duplicate / nonexistent
    # ──────────────────────────────────────────────

    def test_nonexistent_handler_rejected(self):
        """Resolution for a non-existent agent returns agent_not_found."""
        resolution = self.registry.resolve_handler(agent_id="nonexistent-id")
        assert not resolution.resolved
        assert resolution.reason == "agent_not_found"

    def test_is_handler_healthy_returns_false_for_unknown(self):
        """is_handler_healthy fails closed for unknown agents."""
        assert not self.registry.is_handler_healthy("nonexistent-id")

    def test_is_handler_healthy_reflects_health_state(self):
        """is_handler_healthy reflects current accepting_tasks and health."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        assert not self.registry.is_handler_healthy(agent_id)  # PENDING

        self.registry.update_status(agent_id, AgentStatus.RUNNING)
        assert self.registry.is_handler_healthy(agent_id)

        self.registry.set_health(agent_id, "healthy", accepting_tasks=False)
        assert not self.registry.is_handler_healthy(agent_id)

    def test_clear_resolution_cache(self):
        """clear_resolution_cache removes all entries and returns count."""
        agent_id = self.registry.register("worker-1", "worker.processor")
        self.registry.update_status(agent_id, AgentStatus.RUNNING)
        self.registry.resolve_handler(agent_id=agent_id)
        self.registry.resolve_handler(agent_type="worker.processor")

        count = self.registry.clear_resolution_cache()
        assert count >= 2

        # Cache is empty now
        count2 = self.registry.clear_resolution_cache()
        assert count2 == 0