"""Retention deletion workflow — cascade deletion to derived embeddings.

Implements a DeletionManifest that enumerates primary and derived stores,
per-store CompletionRecord verification, and reconciliation for stale derived
records whose source artifacts no longer exist.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.common.metrics import metrics


# ── Data classification constants ────────────────────────────────────────────

PRIMARY_ARTIFACTS = "primary_artifacts"
DERIVED_EMBEDDINGS = "derived_embeddings"
VECTOR_INDEX = "vector_index"
SEARCH_INDEX = "search_index"

ALL_DATA_CLASSES = frozenset(
    {PRIMARY_ARTIFACTS, DERIVED_EMBEDDINGS, VECTOR_INDEX, SEARCH_INDEX}
)

DEFAULT_DERIVED_STORES = frozenset(
    {DERIVED_EMBEDDINGS, VECTOR_INDEX, SEARCH_INDEX}
)


# ── Core data structures ─────────────────────────────────────────────────────


@dataclass
class DataRecord:
    """A single record in a retention store."""

    id: str
    workspace_id: str
    data_class: str
    payload: Dict[str, Any] = field(default_factory=dict)
    source_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class ManifestEntry:
    """A single entry in a deletion manifest pointing at records to delete."""

    store_name: str
    data_class: str
    record_ids: List[str]
    source_ids: List[str] = field(default_factory=list)


@dataclass
class DeletionManifest:
    """A manifest authorising deletion across primary and derived stores."""

    id: str
    workspace_id: str
    reason: str
    entries: List[ManifestEntry]
    created_at: float = field(default_factory=time.time)


@dataclass
class CompletionRecord:
    """Result of applying a manifest entry to a single store."""

    manifest_id: str
    store_name: str
    data_class: str
    requested_ids: List[str]
    deleted_ids: List[str]
    missing_ids: List[str]
    remaining_records: List[str]
    verified: bool
    completed_at: float = field(default_factory=time.time)


# ── Retention store ──────────────────────────────────────────────────────────


class RetentionStore:
    """In-memory store for primary artifacts or derived data.

    Tracks workspace-scoped records with optional source_id linking so
    derived stores can be correlated with their parent artifacts.
    """

    def __init__(
        self,
        name: str,
        data_class: str,
        derived: bool = False,
    ) -> None:
        self.name = name
        self.data_class = data_class
        self.derived = derived
        self._records: Dict[str, DataRecord] = {}

    # ── CRUD ─────────────────────────────────────────────────────────────

    def put(
        self,
        record_id: str,
        workspace_id: str,
        payload: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
    ) -> DataRecord:
        record = DataRecord(
            id=record_id,
            workspace_id=workspace_id,
            data_class=self.data_class,
            payload=payload or {},
            source_id=source_id,
        )
        self._records[record_id] = record
        return record

    def get(self, record_id: str) -> Optional[DataRecord]:
        return self._records.get(record_id)

    def count(self) -> int:
        return len(self._records)

    # ── Query helpers ────────────────────────────────────────────────────

    def ids_for_workspace(self, workspace_id: str) -> List[str]:
        """Return sorted ids of all records in *workspace_id*."""
        return sorted(
            record.id
            for record in self._records.values()
            if record.workspace_id == workspace_id
        )

    def ids_for_sources(
        self,
        workspace_id: str,
        source_ids: Iterable[str],
    ) -> List[str]:
        """Return sorted ids of records whose source_id is in *source_ids*."""
        source_set = set(source_ids)
        return sorted(
            record.id
            for record in self._records.values()
            if (
                record.workspace_id == workspace_id
                and record.source_id in source_set
            )
        )

    def source_ids_for_workspace(self, workspace_id: str) -> Set[str]:
        """Return the set of distinct source_ids in *workspace_id*."""
        return {
            record.source_id or ""
            for record in self._records.values()
            if record.workspace_id == workspace_id
        }

    def stale_source_pairs(
        self,
        workspace_id: str,
        valid_source_ids: Iterable[str],
    ) -> List[Tuple[str, str]]:
        """Return (record_id, source_id) pairs whose source is NOT valid."""
        valid_sources = set(valid_source_ids)
        return sorted(
            (record.id, record.source_id or "")
            for record in self._records.values()
            if (
                record.workspace_id == workspace_id
                and record.source_id not in valid_sources
            )
        )

    # ── Mutation ─────────────────────────────────────────────────────────

    def delete_many(
        self,
        record_ids: Iterable[str],
    ) -> Tuple[List[str], List[str]]:
        """Delete records by id. Returns (deleted_ids, missing_ids)."""
        deleted: List[str] = []
        missing: List[str] = []
        for record_id in record_ids:
            if record_id in self._records:
                self._records.pop(record_id)
                deleted.append(record_id)
            else:
                missing.append(record_id)
        return deleted, missing

    def remaining_ids(self, record_ids: Iterable[str]) -> List[str]:
        """Of *record_ids*, which still exist in the store (sorted)."""
        return sorted(
            record_id
            for record_id in record_ids
            if record_id in self._records
        )


# ── Retention workflow ───────────────────────────────────────────────────────


class RetentionWorkflow:
    """Orchestrates cascade deletion, manifest tracking, and reconciliation.

    Usage::

        wf = RetentionWorkflow(stores=[embeddings_store, vector_store, ...])
        manifest, completions = wf.delete_workspace_data(
            "ws-1", ["artifact-a", "artifact-b"]
        )
        assert all(c.verified for c in completions)
    """

    def __init__(self, stores: Optional[List[RetentionStore]] = None) -> None:
        self.primary_store = RetentionStore("task_artifacts", PRIMARY_ARTIFACTS)
        self._stores: Dict[str, RetentionStore] = {
            self.primary_store.name: self.primary_store,
        }
        self._completion_records: List[CompletionRecord] = []
        self._audit_records: List[Dict[str, Any]] = []

        for store in stores or []:
            self.register_store(store)

    # ── Store management ─────────────────────────────────────────────────

    def register_store(self, store: RetentionStore) -> None:
        self._stores[store.name] = store

    @property
    def stores(self) -> Dict[str, RetentionStore]:
        return dict(self._stores)

    @property
    def completion_records(self) -> List[CompletionRecord]:
        return list(self._completion_records)

    @property
    def audit_records(self) -> List[Dict[str, Any]]:
        return list(self._audit_records)

    # ── Manifest creation ────────────────────────────────────────────────

    def create_deletion_manifest(
        self,
        workspace_id: str,
        artifact_ids: Iterable[str],
        reason: str = "retention_delete",
    ) -> DeletionManifest:
        """Build a manifest covering primary + derived records for *artifact_ids*."""
        source_ids = sorted(set(artifact_ids))
        entries: List[ManifestEntry] = [
            ManifestEntry(
                store_name=self.primary_store.name,
                data_class=self.primary_store.data_class,
                record_ids=source_ids,
                source_ids=source_ids,
            ),
        ]

        for store in self._derived_stores():
            record_ids = store.ids_for_sources(workspace_id, source_ids)
            entries.append(
                ManifestEntry(
                    store_name=store.name,
                    data_class=store.data_class,
                    record_ids=record_ids,
                    source_ids=source_ids,
                )
            )

        manifest = DeletionManifest(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            reason=reason,
            entries=entries,
        )
        self._audit(
            "manifest_created",
            manifest,
            {"entry_count": len(entries)},
        )
        return manifest

    # ── Manifest application ─────────────────────────────────────────────

    def apply_manifest(
        self,
        manifest: DeletionManifest,
    ) -> List[CompletionRecord]:
        """Execute a deletion manifest.

        Returns per-store completion records.
        """
        completions: List[CompletionRecord] = []
        for entry in manifest.entries:
            store = self._stores[entry.store_name]
            deleted, missing = store.delete_many(entry.record_ids)
            remaining = store.remaining_ids(entry.record_ids)
            completion = CompletionRecord(
                manifest_id=manifest.id,
                store_name=entry.store_name,
                data_class=entry.data_class,
                requested_ids=list(entry.record_ids),
                deleted_ids=deleted,
                missing_ids=missing,
                remaining_records=remaining,
                verified=not remaining,
            )
            completions.append(completion)
            self._completion_records.append(completion)

            # Emit sanitized metrics
            metrics.increment(
                f"retention.delete.{entry.data_class}.completed"
            )
            if remaining:
                metrics.increment(
                    f"retention.delete.{entry.data_class}.remaining"
                )

        self._audit(
            "manifest_applied",
            manifest,
            {"completion_count": len(completions)},
        )
        return completions

    # ── Convenience: single-call delete ──────────────────────────────────

    def delete_workspace_data(
        self,
        workspace_id: str,
        artifact_ids: Iterable[str],
        reason: str = "retention_delete",
    ) -> Tuple[DeletionManifest, List[CompletionRecord]]:
        """Create and apply a manifest in one call.

        Returns (manifest, completions).
        """
        manifest = self.create_deletion_manifest(
            workspace_id, artifact_ids, reason=reason
        )
        return manifest, self.apply_manifest(manifest)

    # ── Reconciliation ───────────────────────────────────────────────────

    def reconcile_derived_data(
        self,
        workspace_id: str,
    ) -> Tuple[DeletionManifest, List[CompletionRecord]]:
        """Detect and remove stale derived records whose source artifacts are gone.

        Returns a reconcile manifest and its completion records.  Empty derived
        stores still produce verified completion records so every affected data
        class is accounted for.
        """
        valid_sources = self.primary_store.ids_for_workspace(workspace_id)
        entries: List[ManifestEntry] = []
        stale_source_count = 0

        for store in self._derived_stores():
            stale_pairs = store.stale_source_pairs(workspace_id, valid_sources)
            record_ids = [record_id for record_id, _ in stale_pairs]
            source_ids = sorted(
                {source_id for _, source_id in stale_pairs if source_id}
            )
            stale_source_count += len(source_ids)
            entries.append(
                ManifestEntry(
                    store_name=store.name,
                    data_class=store.data_class,
                    record_ids=record_ids,
                    source_ids=source_ids,
                )
            )

        manifest = DeletionManifest(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            reason="retention_reconcile_stale_derived",
            entries=entries,
        )
        self._audit(
            "reconciliation_manifest_created",
            manifest,
            {"stale_source_count": stale_source_count},
        )
        metrics.increment("retention.reconcile.runs")

        return manifest, self.apply_manifest(manifest)

    def reconcile_all_workspaces(
        self,
    ) -> Dict[str, Tuple[DeletionManifest, List[CompletionRecord]]]:
        """Run reconciliation across every workspace in the primary store."""
        workspace_ids: Set[str] = set()
        for record in self.primary_store._records.values():
            workspace_ids.add(record.workspace_id)

        results: Dict[str, Tuple[DeletionManifest, List[CompletionRecord]]] = {}
        for ws_id in sorted(workspace_ids):
            results[ws_id] = self.reconcile_derived_data(ws_id)
        return results

    # ── Internals ────────────────────────────────────────────────────────

    def _derived_stores(self) -> List[RetentionStore]:
        return sorted(
            (store for store in self._stores.values() if store.derived),
            key=lambda s: s.name,
        )

    def _audit(
        self,
        decision: str,
        manifest: DeletionManifest,
        details: Dict[str, Any],
    ) -> None:
        """Record a sanitized audit event — no secrets, payloads, or private data."""
        self._audit_records.append(
            {
                "decision": decision,
                "manifest_id": manifest.id,
                "workspace_id": manifest.workspace_id,
                "reason": manifest.reason,
                "details": details,
                "timestamp": time.time(),
            }
        )
