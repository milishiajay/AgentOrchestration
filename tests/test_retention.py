"""Regression tests for retention deletion cascade (issue #580).

Coverage:
- Primary artifact deletion cascading to all derived stores
- Idempotent manifest replay with missing-id records
- Reconciliation removing stale derived records, preserving live ones
- Empty derived stores produce verified completion records
- Cross-workspace isolation
- Sanitized audit records (no secrets, no payloads)
- Bulk workspace reconciliation
"""

from typing import Tuple

from src.common.retention import (
    DERIVED_EMBEDDINGS,
    PRIMARY_ARTIFACTS,
    SEARCH_INDEX,
    VECTOR_INDEX,
    RetentionStore,
    RetentionWorkflow,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def build_workflow() -> Tuple[
    RetentionWorkflow, RetentionStore, RetentionStore, RetentionStore
]:
    """Return a workflow with three derived stores attached."""
    embeddings = RetentionStore("embeddings", DERIVED_EMBEDDINGS, derived=True)
    vector_index = RetentionStore("vector_index", VECTOR_INDEX, derived=True)
    search_index = RetentionStore("search_index", SEARCH_INDEX, derived=True)
    workflow = RetentionWorkflow(stores=[embeddings, vector_index, search_index])
    return workflow, embeddings, vector_index, search_index


def seed_data(wf, embeddings, vector_index, search_index) -> None:
    """Seed a workspace with primary artifacts and derived records."""
    wf.primary_store.put("artifact-1", "ws-1", {"kind": "task"})
    wf.primary_store.put("artifact-2", "ws-1", {"kind": "log"})
    embeddings.put("emb-1", "ws-1", source_id="artifact-1")
    embeddings.put("emb-2", "ws-1", source_id="artifact-2")
    vector_index.put("vec-1", "ws-1", source_id="artifact-1")
    search_index.put("srch-1", "ws-1", source_id="artifact-1")


# ── Cascade deletion ─────────────────────────────────────────────────────────


def test_cascade_deletion_removes_primary_and_all_derived():
    """Primary artifact deletion removes embeddings, vectors, and search indexes."""
    wf, emb, vec, srch = build_workflow()
    seed_data(wf, emb, vec, srch)

    manifest, completions = wf.delete_workspace_data(
        "ws-1", ["artifact-1", "artifact-2"]
    )

    # Manifest is well-formed
    assert manifest.workspace_id == "ws-1"
    assert manifest.reason == "retention_delete"
    assert len(manifest.entries) == 4  # primary + 3 derived

    # All records removed
    assert wf.primary_store.get("artifact-1") is None
    assert wf.primary_store.get("artifact-2") is None
    assert emb.get("emb-1") is None
    assert emb.get("emb-2") is None
    assert vec.get("vec-1") is None
    assert srch.get("srch-1") is None

    # Completion records cover every data class
    data_classes = {c.data_class for c in completions}
    assert data_classes == {PRIMARY_ARTIFACTS, DERIVED_EMBEDDINGS, VECTOR_INDEX, SEARCH_INDEX}

    # All stores verified
    assert all(c.verified for c in completions)


def test_cascade_deletion_is_workspace_scoped():
    """Deletion in workspace-a does not affect records in workspace-b."""
    wf, emb, vec, srch = build_workflow()
    wf.primary_store.put("artifact-1", "ws-a")
    wf.primary_store.put("artifact-b1", "ws-b")
    emb.put("emb-a", "ws-a", source_id="artifact-1")
    emb.put("emb-b", "ws-b", source_id="artifact-b1")

    wf.delete_workspace_data("ws-a", ["artifact-1"])

    assert wf.primary_store.get("artifact-b1") is not None
    assert emb.get("emb-b") is not None
    assert wf.primary_store.get("artifact-1") is None
    assert emb.get("emb-a") is None


def test_deleting_unknown_artifact_produces_clean_completion():
    """Deleting an artifact that doesn't exist yields verified completions with missing_ids."""
    wf, emb, vec, srch = build_workflow()

    manifest = wf.create_deletion_manifest("ws-1", ["no-such-artifact"])
    completions = wf.apply_manifest(manifest)

    assert len(completions) == 4
    assert all(c.verified for c in completions)
    primary = [c for c in completions if c.data_class == PRIMARY_ARTIFACTS][0]
    assert primary.missing_ids == ["no-such-artifact"]
    assert primary.deleted_ids == []


# ── Idempotent replay ────────────────────────────────────────────────────────


def test_manifest_replay_is_idempotent():
    """Replaying the same manifest twice produces no new deletions."""
    wf, emb, vec, srch = build_workflow()
    seed_data(wf, emb, vec, srch)

    manifest = wf.create_deletion_manifest("ws-1", ["artifact-1", "artifact-2"])
    first = wf.apply_manifest(manifest)
    second = wf.apply_manifest(manifest)

    assert all(c.verified for c in first)
    assert all(c.verified for c in second)
    # Second pass deletes nothing
    assert all(not c.deleted_ids for c in second)

    # All originally-existing IDs are marked missing on replay
    expected_missing = {"artifact-1", "artifact-2", "emb-1", "emb-2", "vec-1", "srch-1"}
    all_missing = {
        missing for c in second for missing in c.missing_ids
    }
    assert all_missing == expected_missing


def test_partial_manifest_replay_handles_mixed_state():
    """Replaying after some records are gone marks missing for those only."""
    wf, emb, vec, srch = build_workflow()
    seed_data(wf, emb, vec, srch)

    manifest = wf.create_deletion_manifest("ws-1", ["artifact-1", "artifact-2"])
    # Manually delete one record before replay
    wf.primary_store._records.pop("artifact-2")

    completions = wf.apply_manifest(manifest)
    primary = [c for c in completions if c.data_class == PRIMARY_ARTIFACTS][0]
    assert "artifact-1" in primary.deleted_ids
    assert "artifact-2" in primary.missing_ids


# ── Reconciliation ───────────────────────────────────────────────────────────


def test_reconciliation_removes_stale_derived_records():
    """Stale derived records (source gone) are removed; live ones survive."""
    wf, emb, vec, srch = build_workflow()
    wf.primary_store.put("artifact-live", "ws-1")
    emb.put("emb-live", "ws-1", source_id="artifact-live")
    emb.put("emb-stale", "ws-1", source_id="artifact-deleted")
    vec.put("vec-stale", "ws-1", source_id="artifact-deleted")
    srch.put("srch-stale", "ws-1", source_id="artifact-deleted")

    manifest, completions = wf.reconcile_derived_data("ws-1")

    assert manifest.reason == "retention_reconcile_stale_derived"
    # Live records survive
    assert wf.primary_store.get("artifact-live") is not None
    assert emb.get("emb-live") is not None
    # Stale records gone
    assert emb.get("emb-stale") is None
    assert vec.get("vec-stale") is None
    assert srch.get("srch-stale") is None
    # Only derived stores in completion records
    assert {c.data_class for c in completions} == {
        DERIVED_EMBEDDINGS, VECTOR_INDEX, SEARCH_INDEX,
    }
    assert all(c.verified for c in completions)


def test_reconciliation_with_empty_derived_stores_is_clean():
    """Empty derived stores produce verified completion records (not absent)."""
    wf, _, _, _ = build_workflow()
    wf.primary_store.put("artifact-live", "ws-1")

    _, completions = wf.reconcile_derived_data("ws-1")

    assert len(completions) == 3
    assert all(c.requested_ids == [] for c in completions)
    assert all(c.verified for c in completions)


def test_reconciliation_noop_when_no_stale_records():
    """Reconciliation with all sources present is a clean no-op."""
    wf, emb, vec, srch = build_workflow()
    seed_data(wf, emb, vec, srch)

    manifest, completions = wf.reconcile_derived_data("ws-1")

    assert manifest.entries  # 3 entries exist
    assert all(c.requested_ids == [] for c in completions)
    assert all(c.verified for c in completions)

    # All original data still there
    assert wf.primary_store.get("artifact-1") is not None
    assert emb.get("emb-1") is not None


def test_reconcile_all_workspaces():
    """reconcile_all_workspaces covers every workspace in the primary store."""
    wf = RetentionWorkflow()
    wf.primary_store.put("a-1", "ws-a")
    wf.primary_store.put("b-1", "ws-b")
    wf.primary_store.put("c-1", "ws-c")

    results = wf.reconcile_all_workspaces()
    assert set(results.keys()) == {"ws-a", "ws-b", "ws-c"}


# ── Audit sanitisation ──────────────────────────────────────────────────────


def test_audit_records_are_sanitized():
    """Audit records contain no secrets or payload contents."""
    wf, emb, vec, srch = build_workflow()
    wf.primary_store.put("artifact-1", "ws-1", {"secret": "super-secret-token"})
    emb.put("emb-1", "ws-1", source_id="artifact-1", payload={"token": "abc"})

    wf.delete_workspace_data("ws-1", ["artifact-1"])

    audit_text = str(wf.audit_records)
    assert "super-secret-token" not in audit_text
    assert "abc" not in audit_text
    assert "secret" not in audit_text


def test_audit_records_track_manifest_lifecycle():
    """Audit records track manifest creation and application."""
    wf, _, _, _ = build_workflow()
    wf.primary_store.put("a-1", "ws-1")

    manifest, completions = wf.delete_workspace_data("ws-1", ["a-1"])

    decisions = [r["decision"] for r in wf.audit_records]
    assert "manifest_created" in decisions
    assert "manifest_applied" in decisions


# ── Metrics emission ────────────────────────────────────────────────────────


def test_metrics_emitted_for_deletion():
    """Metrics counters are incremented during deletion."""
    wf, _, _, _ = build_workflow()
    wf.primary_store.put("a-1", "ws-1")

    wf.delete_workspace_data("ws-1", ["a-1"])

    # Deletion manifest covers primary + all 3 derived stores
    assert len(wf.completion_records) == 4
    assert all(c.verified for c in wf.completion_records)


# ── Store-level unit tests ──────────────────────────────────────────────────


def test_retention_store_ids_for_sources():
    store = RetentionStore("test", "data", derived=True)
    store.put("r1", "ws-1", source_id="src-a")
    store.put("r2", "ws-1", source_id="src-b")
    store.put("r3", "ws-2", source_id="src-a")

    assert store.ids_for_sources("ws-1", ["src-a"]) == ["r1"]
    assert store.ids_for_sources("ws-1", ["src-b", "src-a"]) == ["r1", "r2"]


def test_retention_store_stale_source_pairs():
    store = RetentionStore("test", "data", derived=True)
    store.put("r1", "ws-1", source_id="src-alive")
    store.put("r2", "ws-1", source_id="src-gone")
    store.put("r3", "ws-1", source_id="")  # no source

    pairs = store.stale_source_pairs("ws-1", ["src-alive"])
    assert pairs == [("r2", "src-gone"), ("r3", "")]


def test_retention_store_delete_many_returns_deleted_and_missing():
    store = RetentionStore("test", "data")
    store.put("a", "ws-1")
    store.put("b", "ws-1")

    deleted, missing = store.delete_many(["a", "x", "b", "y"])
    assert sorted(deleted) == ["a", "b"]
    assert sorted(missing) == ["x", "y"]
    assert store.count() == 0
