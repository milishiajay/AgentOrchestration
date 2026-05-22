"""Runner provenance integration for the agent module.

Exposes validate_runner_provenance as a preflight hook that can be
wired into the execution pipeline via the orchestration engine hooks.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple


# Mirror constants from the script for import compatibility
APPROVED_RUNNER_LABELS = {
    "self-hosted",
    "linux",
    "x64",
    "arm64",
    "ubuntu-latest",
    "ubuntu-22.04",
    "ubuntu-24.04",
}

APPROVED_IMAGE_DIGESTS: Dict[str, str] = {
    "ubuntu-22.04": "sha256:abc123def4567890abcdef1234567890abcdef1234567890abcdef1234567890",
    "ubuntu-24.04": "sha256:fedcba0987654321fedcba0987654321fedcba0987654321fedcba0987654321",
    "debian-12":    "sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
}

MAX_IMAGE_AGE_SECONDS = int(
    os.environ.get("AO_RUNNER_MAX_IMAGE_AGE", str(30 * 24 * 3600))
)

STRICT_MODE = os.environ.get("AO_RUNNER_STRICT", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Runner provenance hook (for orchestrator integration)
# ---------------------------------------------------------------------------


def runner_provenance_preflight(
    registry: Optional[Any] = None,
    strict: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Preflight check: validates runner image provenance before builds.

    This is designed to be registered as a 'pre_execute' hook on the
    OrchestrationEngine for release jobs.

    Returns:
        Tuple of (allowed, reason). If allowed is False, the task should
        be failed before execution.
    """
    use_strict = strict if strict is not None else STRICT_MODE

    metadata = _extract_metadata()
    runner_name = metadata.get("runner_name", "unknown")

    checks: List[Tuple[str, bool, str]] = [
        _check_labels(metadata, use_strict),
        _check_digest(metadata, use_strict),
        _check_freshness(metadata, use_strict),
        _check_integrity(metadata, use_strict),
    ]

    failed = [(name, detail) for name, passed, detail in checks if not passed]
    if failed:
        reasons = "; ".join(f"{name}: {detail}" for name, detail in failed)
        return False, f"[{runner_name}] provenance failed: {reasons}"

    return True, f"[{runner_name}] provenance validated"


def _extract_metadata() -> Dict[str, str]:
    metadata: Dict[str, str] = {}
    metadata["runner_name"] = os.environ.get("RUNNER_NAME", os.uname().nodename)
    metadata["runner_labels"] = os.environ.get("RUNNER_LABELS", "")
    metadata["image_os"] = os.environ.get("ImageOS", "")
    metadata["image_name"] = os.environ.get(
        "AO_RUNNER_IMAGE_NAME", metadata["image_os"]
    )
    metadata["image_digest"] = os.environ.get("AO_RUNNER_IMAGE_DIGEST", "")
    metadata["image_build_timestamp"] = os.environ.get(
        "AO_RUNNER_IMAGE_BUILD_TS", ""
    )
    metadata["runner_checksum"] = os.environ.get("AO_RUNNER_CHECKSUM", "")
    return metadata


def _check_labels(
    metadata: Dict[str, str], strict: bool
) -> Tuple[str, bool, str]:
    labels_raw = metadata.get("runner_labels", "")
    if not labels_raw:
        return ("approved_runner_labels", not strict, "No runner labels found")
    labels = {lbl.strip().lower() for lbl in labels_raw.split(",") if lbl.strip()}
    if not labels:
        return ("approved_runner_labels", not strict, "Empty runner labels")
    unapproved = labels - APPROVED_RUNNER_LABELS
    if unapproved:
        return (
            "approved_runner_labels",
            False,
            f"Unapproved labels: {sorted(unapproved)}",
        )
    return ("approved_runner_labels", True, f"{len(labels)} labels approved")


def _check_digest(
    metadata: Dict[str, str], strict: bool
) -> Tuple[str, bool, str]:
    image_name = metadata.get("image_name", "").strip()
    image_digest = metadata.get("image_digest", "").strip()
    if not image_digest:
        return ("approved_image_digest", not strict, "No image digest")
    if not _valid_digest(image_digest):
        return ("approved_image_digest", False, "Invalid digest format")
    if image_name and image_name in APPROVED_IMAGE_DIGESTS:
        expected = APPROVED_IMAGE_DIGESTS[image_name]
        if image_digest == expected:
            return ("approved_image_digest", True, "Digest approved")
        return ("approved_image_digest", False, "Digest not in allowlist")
    return (
        "approved_image_digest",
        not strict,
        "Digest valid but not in allowlist" if strict else "Digest format valid",
    )


def _check_freshness(
    metadata: Dict[str, str], strict: bool
) -> Tuple[str, bool, str]:
    build_ts = metadata.get("image_build_timestamp", "").strip()
    if not build_ts:
        return ("image_build_freshness", not strict, "No build timestamp")
    build_time = _parse_ts(build_ts)
    if build_time is None:
        return ("image_build_freshness", False, f"Cannot parse: {build_ts}")
    age = (datetime.now(timezone.utc) - build_time).total_seconds()
    if age > MAX_IMAGE_AGE_SECONDS:
        return (
            "image_build_freshness",
            False,
            f"Image is {age / 86400:.1f} days old (max {MAX_IMAGE_AGE_SECONDS / 86400:.0f})",
        )
    return ("image_build_freshness", True, f"Built {age / 86400:.1f} days ago")


def _check_integrity(
    metadata: Dict[str, str], strict: bool
) -> Tuple[str, bool, str]:
    checksum = metadata.get("runner_checksum", "").strip()
    if not checksum:
        return ("runner_checksum", not strict, "No checksum provided")
    fields = [
        metadata.get("runner_name", ""),
        metadata.get("image_name", ""),
        metadata.get("image_digest", ""),
        metadata.get("image_build_timestamp", ""),
    ]
    expected = "sha256:" + hashlib.sha256("|".join(fields).encode()).hexdigest()
    if checksum == expected:
        return ("runner_checksum", True, "Checksum matches")
    return ("runner_checksum", False, "Checksum mismatch")


def _valid_digest(digest: str) -> bool:
    if ":" not in digest:
        return False
    algo, _, hex_part = digest.partition(":")
    if not algo or not hex_part or len(hex_part) < 32:
        return False
    try:
        int(hex_part, 16)
    except ValueError:
        return False
    return True


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts.replace("Z", "+00:00"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None
