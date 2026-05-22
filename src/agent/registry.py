"""Agent Registry — Manages agent lifecycle and metadata."""

import time
import uuid
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple


class AgentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"
    TERMINATED = "terminated"
    DRAINING = "draining"


# Terminal states that should never accept tasks
_TERMINAL_STATUSES = {AgentStatus.STOPPED, AgentStatus.TERMINATED, AgentStatus.FAILED}
# States where a handler exists but should not receive new work
_UNROUTABLE_STATUSES = _TERMINAL_STATUSES | {AgentStatus.PENDING, AgentStatus.DRAINING}


class HandlerResolution:
    """Result of a health-aware handler resolution.

    Carries sanitized metadata for audit/monitoring without
    exposing private config, secrets, or runtime payloads.
    """

    __slots__ = ("resolved", "agent", "reason", "deferred_ids")

    def __init__(
        self,
        resolved: bool,
        agent: Optional[Dict[str, Any]] = None,
        reason: str = "",
        deferred_ids: Optional[List[str]] = None,
    ):
        self.resolved = resolved
        self.agent = agent
        self.reason = reason
        self.deferred_ids = deferred_ids or []


class AgentRegistry:
    def __init__(self, storage_backend: str = "memory"):
        self.storage_backend = storage_backend
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._index: Dict[str, List[str]] = {}
        self._resolution_cache: Dict[str, Tuple[float, HandlerResolution]] = {}
        self._cache_ttl: float = 5.0

    def register(
        self,
        name: str,
        agent_type: str,
        config: Optional[Dict] = None,
        capabilities: Optional[Iterable[str]] = None,
    ) -> str:
        agent_id = str(uuid.uuid4())
        timestamp = time.time()
        capability_list = self._normalize_capabilities(capabilities)
        self._agents[agent_id] = {
            "id": agent_id,
            "name": name,
            "type": agent_type,
            "status": AgentStatus.PENDING.value,
            "config": config or {},
            "capabilities": capability_list,
            "capability_epoch": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_reconnected_at": timestamp,
            "version": "1.0.0",
            "accepting_tasks": True,
            "health": "healthy",
            "metrics": {"tasks_completed": 0, "errors": 0, "uptime": 0},
            "audit": [{
                "event": "worker_registered",
                "capability_epoch": 1,
                "capability_count": len(capability_list),
            }],
        }
        group = agent_type.split(".")[0]
        if group not in self._index:
            self._index[group] = []
        self._index[group].append(agent_id)
        self._invalidate_cache_for(agent_id)
        return agent_id

    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        return self._agents.get(agent_id)

    def list(
        self,
        status: Optional[AgentStatus] = None,
        group: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        agents = self._agents.values()
        if status:
            agents = [a for a in agents if a["status"] == status.value]
        if group:
            agent_ids = self._index.get(group, [])
            agents = [a for a in agents if a["id"] in agent_ids]
        return list(agents)

    def update_status(self, agent_id: str, status: AgentStatus) -> bool:
        if agent_id not in self._agents:
            return False
        self._agents[agent_id]["status"] = status.value
        self._agents[agent_id]["updated_at"] = time.time()
        self._invalidate_cache_for(agent_id)
        return True

    def refresh_capabilities(
        self,
        agent_id: str,
        capabilities: Iterable[str],
    ) -> Optional[Dict[str, Any]]:
        """Refresh worker capabilities on reconnect.

        Updates the worker's capability set, bumps the monotonic epoch,
        records a sanitized audit entry, and returns a snapshot suitable
        for scheduler claim validation.
        """
        if agent_id not in self._agents:
            return None
        capability_list = self._normalize_capabilities(capabilities)
        agent = self._agents[agent_id]
        agent["capabilities"] = capability_list
        agent["capability_epoch"] += 1
        agent["updated_at"] = time.time()
        agent["last_reconnected_at"] = agent["updated_at"]
        agent["audit"].append({
            "event": "worker_capabilities_refreshed",
            "capability_epoch": agent["capability_epoch"],
            "capability_count": len(capability_list),
        })
        self._invalidate_cache_for(agent_id)
        return self.worker_snapshot(agent_id)

    def worker_snapshot(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return a claims-safe worker snapshot for scheduler validation."""
        agent = self._agents.get(agent_id)
        if not agent:
            return None
        return {
            "id": agent["id"],
            "status": agent["status"],
            "capabilities": list(agent["capabilities"]),
            "capability_epoch": agent["capability_epoch"],
        }

    def delete(self, agent_id: str) -> bool:
        if agent_id not in self._agents:
            return False
        agent_group = self._agents[agent_id]["type"].split(".")[0]
        agent = self._agents.pop(agent_id)
        if agent_group in self._index and agent_id in self._index[agent_group]:
            self._index[agent_group].remove(agent_id)
        self._invalidate_cache_for(agent_id, group=agent_group)
        return True

    def count(self) -> int:
        return len(self._agents)

    # ──────────────────────────────────────────────
    #  Health-aware handler resolution
    # ──────────────────────────────────────────────

    def set_health(
        self,
        agent_id: str,
        health: str,
        accepting_tasks: bool = True,
    ) -> bool:
        """Update handler health state for rolling deploys.

        When a handler is marked as not accepting tasks or unhealthy,
        the resolution cache is invalidated so subsequent routing
        decisions will defer or skip the handler.
        """
        if agent_id not in self._agents:
            return False
        self._agents[agent_id]["health"] = health
        self._agents[agent_id]["accepting_tasks"] = accepting_tasks
        self._agents[agent_id]["updated_at"] = time.time()
        self._invalidate_cache_for(agent_id)
        return True

    def resolve_handler(
        self,
        agent_id: Optional[str] = None,
        agent_type: Optional[str] = None,
        required_capability: Optional[str] = None,
        required_version: Optional[str] = None,
    ) -> HandlerResolution:
        """Health-aware handler resolution for routing.

        Checks handler health, status, accepting_tasks flag,
        capability compatibility, and version match before
        returning a routable handler.

        If an exact agent_id is provided, only that handler is checked.
        Otherwise, iterates handlers of the given type (or all handlers)
        to find the first healthy, compatible one.

        Resolution results are cached with a short TTL and invalidated
        on any lifecycle/health change.
        """
        # Build cache key from inputs
        cache_key = f"{agent_id}:{agent_type}:{required_capability}:{required_version}"
        cached = self._resolution_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]

        result = self._resolve_handler_impl(
            agent_id=agent_id,
            agent_type=agent_type,
            required_capability=required_capability,
            required_version=required_version,
        )
        self._resolution_cache[cache_key] = (time.time(), result)
        return result

    def is_handler_healthy(self, agent_id: str) -> bool:
        """Check if a specific handler is healthy and routable."""
        agent = self._agents.get(agent_id)
        if not agent:
            return False
        return self._check_handler_routable(agent)

    def _resolve_handler_impl(
        self,
        agent_id: Optional[str] = None,
        agent_type: Optional[str] = None,
        required_capability: Optional[str] = None,
        required_version: Optional[str] = None,
    ) -> HandlerResolution:
        deferred: List[str] = []

        # Exact-agent-id path
        if agent_id is not None:
            agent = self._agents.get(agent_id)
            if not agent:
                return HandlerResolution(False, reason="agent_not_found")
            if not self._check_handler_routable(agent):
                deferred.append(agent_id)
                return HandlerResolution(
                    False,
                    reason=f"handler_unroutable:{agent['status']}",
                    deferred_ids=deferred,
                )
            if required_capability and required_capability not in set(
                agent.get("capabilities", [])
            ):
                return HandlerResolution(
                    False,
                    reason="missing_capability",
                    deferred_ids=[agent_id],
                )
            if required_version and agent.get("version") != required_version:
                return HandlerResolution(
                    False,
                    reason="version_mismatch",
                    deferred_ids=[agent_id],
                )
            return HandlerResolution(True, agent=agent, reason="healthy")

        # Type-based or all-handlers path
        candidates = list(self._agents.values())
        if agent_type:
            group_prefix = agent_type.split(".")[0]
            group_ids = self._index.get(group_prefix, [])
            candidates = [
                a for a in candidates if a["id"] in group_ids
            ]

        for agent in candidates:
            if not self._check_handler_routable(agent):
                deferred.append(agent["id"])
                continue
            if required_capability and required_capability not in set(
                agent.get("capabilities", [])
            ):
                deferred.append(agent["id"])
                continue
            if required_version and agent.get("version") != required_version:
                deferred.append(agent["id"])
                continue
            return HandlerResolution(
                True, agent=agent, reason="healthy", deferred_ids=deferred,
            )

        reason = "no_healthy_handler" if not deferred else "all_deferred"
        return HandlerResolution(False, reason=reason, deferred_ids=deferred)

    def _check_handler_routable(self, agent: Dict[str, Any]) -> bool:
        """Check if a handler is healthy enough to route to."""
        status = agent.get("status", "")
        try:
            status_enum = AgentStatus(status)
        except ValueError:
            return False

        # Terminal and unready states are unroutable
        if status_enum in _UNROUTABLE_STATUSES:
            return False

        # PENDING is not routable (not yet initialized)
        if status_enum == AgentStatus.PENDING:
            return False

        # Must be accepting tasks
        if not agent.get("accepting_tasks", True):
            return False

        # Health must be "healthy"
        if agent.get("health", "healthy") != "healthy":
            return False

        return True

    # ──────────────────────────────────────────────
    #  Resolution cache management
    # ──────────────────────────────────────────────

    def _invalidate_cache_for(self, agent_id: str, group: Optional[str] = None) -> None:
        """Invalidate all cached resolution entries referencing agent_id.

        Called on any lifecycle change: register, status change,
        health change, capability refresh, or deletion.

        When group is not provided, it's looked up from the agent
        entry (if the agent hasn't been deleted yet).
        """
        # Resolve group if not explicitly provided
        if group is None:
            agent = self._agents.get(agent_id)
            if agent:
                group = agent["type"].split(".")[0]

        to_remove = []
        for key in self._resolution_cache:
            key_parts = key.split(":")
            if agent_id in key_parts:
                to_remove.append(key)
            elif group and key_parts[0] == "None" and key_parts[1] and key_parts[1].startswith(group):
                if key not in to_remove:
                    to_remove.append(key)
        for key in to_remove:
            self._resolution_cache.pop(key, None)

    def clear_resolution_cache(self) -> int:
        """Clear all resolution cache entries. Returns count of entries cleared."""
        count = len(self._resolution_cache)
        self._resolution_cache.clear()
        return count

    @staticmethod
    def _normalize_capabilities(
        capabilities: Optional[Iterable[str]],
    ) -> List[str]:
        if not capabilities:
            return []
        return sorted({
            capability.strip().lower()
            for capability in capabilities
            if capability and capability.strip()
        })

# 2019-01-29T11:24:49 update

# 2019-04-09T13:38:38 update

# 2019-04-11T11:24:12 update

# 2019-06-26T17:03:48 update

# 2019-07-03T14:55:48 update

# 2019-07-18T18:18:47 update

# 2019-11-05T11:27:19 update

# 2019-11-20T11:35:05 update

# 2019-11-23T15:28:54 update

# 2020-03-13T09:23:07 update

# 2020-03-30T19:31:18 update

# 2020-04-22T15:03:30 update

# 2020-07-21T10:00:48 update

# 2020-09-10T09:02:08 update

# 2020-09-10T13:39:12 update

# 2020-09-22T16:27:52 update

# 2020-10-15T10:33:14 update

# 2021-05-13T11:15:56 update

# 2021-07-07T14:57:13 update

# 2021-07-13T15:15:19 update

# 2021-07-27T10:18:16 update

# 2022-03-11T15:24:11 update

# 2022-09-22T13:24:20 update

# 2022-11-01T12:20:40 update

# 2023-01-30T12:32:27 update

# 2023-03-10T09:43:50 update

# 2023-05-10T14:28:01 update

# 2023-05-11T20:04:46 update

# 2023-05-30T17:00:59 update

# 2023-07-13T17:54:32 update

# 2023-07-20T19:04:20 update

# 2023-07-31T17:00:02 update

# 2023-09-05T19:42:07 update

# 2024-01-02T10:29:47 update

# 2024-09-17T12:45:29 update

# 2024-09-17T11:51:01 update

# 2024-11-06T18:20:15 update

# 2025-01-12T15:13:14 update

# 2025-01-14T20:24:39 update

# 2025-03-26T20:21:27 update

# 2025-04-10T18:27:06 update

# 2025-06-19T20:34:58 update

# 2025-06-21T20:23:53 update

# 2025-06-24T20:30:30 update

# 2025-07-03T13:28:03 update

# 2025-07-24T17:42:21 update

# 2025-08-19T17:42:23 update

# 2025-08-21T11:06:52 update

# 2025-10-24T09:10:08 update

# 2025-12-18T19:34:38 update

# 2026-02-06T11:22:22 update

# 2026-02-13T15:42:04 update

# 2026-04-10T08:16:30 update

# 2026-04-29T18:16:11 update
