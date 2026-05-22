#!/usr/bin/env python3
"""CI Runner Image Provenance Validator.

Validates that self-hosted CI runner images come from approved sources
by checking image digest, build timestamp, and approved runner labels.

Designed to run as a preflight check before build/release steps.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

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

# Maximum allowed image age in seconds (default: 30 days)
MAX_IMAGE_AGE_SECONDS = int(
    os.environ.get("AO_RUNNER_MAX_IMAGE_AGE", str(30 * 24 * 3600))
)

# Strict mode: fail closed when provenance metadata is missing
STRICT_MODE = os.environ.get("AO_RUNNER_STRICT", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProvenanceResult:
    """Result of a single provenance check."""

    check: str
    passed: bool
    detail: str
    evidence: Optional[str] = None


@dataclass
class ProvenanceReport:
    """Aggregate provenance validation report."""

    overall_passed: bool
    runner_name: str
    results: List[ProvenanceResult] = field(default_factory=list)
    checked_at: str = ""

    def __post_init__(self) -> None:
        if not self.checked_at:
            self.checked_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Provenance metadata extraction
# ---------------------------------------------------------------------------

def _read_env(key: str, default: str = "") -> str:
    """Read an environment variable, stripping whitespace."""
    return os.environ.get(key, default).strip()


def _parse_iso_timestamp(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returning None on failure."""
    if not ts:
        return None
    try:
        # Try ISO format first
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass
    # Try common alternate formats
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(ts.replace("Z", "+00:00"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def extract_runner_metadata() -> Dict[str, str]:
    """Collect runner image provenance metadata from the environment.

    Sources:
    - GITHUB_* env vars (GitHub Actions)
    - AO_RUNNER_* env vars (custom self-hosted metadata)
    - /etc/runner-image.json (optional metadata file)
    """
    metadata: Dict[str, str] = {}

    # GitHub Actions runner context
    metadata["runner_name"] = _read_env("RUNNER_NAME", os.uname().nodename)
    metadata["runner_os"] = _read_env("RUNNER_OS", sys.platform)
    metadata["runner_labels"] = _read_env("RUNNER_LABELS", "")
    metadata["image_os"] = _read_env("ImageOS", "")

    # Image digest (can be set by self-hosted runner setup)
    metadata["image_digest"] = _read_env("AO_RUNNER_IMAGE_DIGEST")
    metadata["image_name"] = _read_env("AO_RUNNER_IMAGE_NAME", metadata["image_os"])
    metadata["image_build_timestamp"] = _read_env("AO_RUNNER_IMAGE_BUILD_TS")
    metadata["image_signed_by"] = _read_env("AO_RUNNER_IMAGE_SIGNED_BY")

    # Optional metadata file
    metadata_file = os.environ.get(
        "AO_RUNNER_METADATA_FILE", "/etc/runner-image.json"
    )
    if os.path.isfile(metadata_file):
        try:
            with open(metadata_file) as f:
                file_metadata = json.load(f)
            for k, v in file_metadata.items():
                if isinstance(v, str):
                    # Only override if not already set via env
                    metadata.setdefault(k, v)
        except (json.JSONDecodeError, OSError):
            pass

    # Runner-provided checksum
    metadata["runner_checksum"] = _read_env("AO_RUNNER_CHECKSUM")

    return metadata


# ---------------------------------------------------------------------------
# Provenance checks
# ---------------------------------------------------------------------------

def check_runner_labels(
    metadata: Dict[str, str],
) -> ProvenanceResult:
    """Validate that the runner's labels are in the approved set.

    Unapproved labels cause a failure if strict mode is on or if
    the runner is self-hosted.
    """
    labels_raw = metadata.get("runner_labels", "")
    if not labels_raw:
        return ProvenanceResult(
            check="approved_runner_labels",
            passed=not STRICT_MODE,
            detail="No runner labels found; cannot validate",
            evidence="",
        )

    labels = {lbl.strip().lower() for lbl in labels_raw.split(",") if lbl.strip()}
    if not labels:
        return ProvenanceResult(
            check="approved_runner_labels",
            passed=not STRICT_MODE,
            detail="Runner labels empty after parsing",
            evidence="",
        )

    unapproved = labels - APPROVED_RUNNER_LABELS
    if unapproved:
        return ProvenanceResult(
            check="approved_runner_labels",
            passed=False,
            detail=f"Unapproved runner labels: {sorted(unapproved)}",
            evidence=f"labels={labels_raw}",
        )

    return ProvenanceResult(
        check="approved_runner_labels",
        passed=True,
        detail=f"All {len(labels)} runner labels are approved",
        evidence=f"labels={labels_raw}",
    )


def check_image_digest(
    metadata: Dict[str, str],
) -> ProvenanceResult:
    """Validate that the runner image digest matches an approved value.

    If the image name matches an entry in APPROVED_IMAGE_DIGESTS,
    the digest must match exactly. For self-hosted runners with
    AO_RUNNER_IMAGE_DIGEST set, the digest is validated against
    the allowlist or at minimum verified for structural validity.
    """
    image_name = metadata.get("image_name", "").strip()
    image_digest = metadata.get("image_digest", "").strip()

    if not image_digest:
        return ProvenanceResult(
            check="approved_image_digest",
            passed=not STRICT_MODE,
            detail="No image digest available; cannot validate image identity",
            evidence="",
        )

    # Validate digest format (must be sha256:hex or similar)
    if not _is_valid_digest_format(image_digest):
        return ProvenanceResult(
            check="approved_image_digest",
            passed=False,
            detail=f"Image digest has invalid format: {image_digest[:32]}...",
            evidence=f"digest={image_digest}",
        )

    # Check against explicit allowlist
    if image_name and image_name in APPROVED_IMAGE_DIGESTS:
        expected = APPROVED_IMAGE_DIGESTS[image_name]
        if image_digest == expected:
            return ProvenanceResult(
                check="approved_image_digest",
                passed=True,
                detail=f"Image digest matches approved entry for {image_name}",
                evidence=f"digest={image_digest}",
            )
        return ProvenanceResult(
            check="approved_image_digest",
            passed=False,
            detail=(
                f"Image digest for {image_name} does not match approved value"
            ),
            evidence=f"got={image_digest}, expected={expected}",
        )

    # Self-hosted: digest format valid but not in explicit allowlist
    # Accept if strict mode is off and format is valid
    return ProvenanceResult(
        check="approved_image_digest",
        passed=not STRICT_MODE,
        detail=(
            f"Image digest has valid format but is not in explicit allowlist"
            if not STRICT_MODE
            else "Image digest not found in allowlist (strict mode)"
        ),
        evidence=f"digest={image_digest}",
    )


def check_image_freshness(
    metadata: Dict[str, str],
) -> ProvenanceResult:
    """Validate that the runner image was built within the allowed age window.

    Uses AO_RUNNER_IMAGE_BUILD_TS and MAX_IMAGE_AGE_SECONDS.
    """
    build_ts = metadata.get("image_build_timestamp", "").strip()
    if not build_ts:
        return ProvenanceResult(
            check="image_build_freshness",
            passed=not STRICT_MODE,
            detail="No image build timestamp available; cannot validate freshness",
            evidence="",
        )

    build_time = _parse_iso_timestamp(build_ts)
    if build_time is None:
        return ProvenanceResult(
            check="image_build_freshness",
            passed=False,
            detail=f"Cannot parse build timestamp: {build_ts}",
            evidence=f"raw={build_ts}",
        )

    age_seconds = (datetime.now(timezone.utc) - build_time).total_seconds()
    if age_seconds > MAX_IMAGE_AGE_SECONDS:
        age_days = age_seconds / 86400
        max_days = MAX_IMAGE_AGE_SECONDS / 86400
        return ProvenanceResult(
            check="image_build_freshness",
            passed=False,
            detail=(
                f"Runner image is {age_days:.1f} days old "
                f"(max allowed: {max_days:.0f} days)"
            ),
            evidence=f"build_ts={build_ts}, age_seconds={age_seconds:.0f}",
        )

    age_days = age_seconds / 86400
    return ProvenanceResult(
        check="image_build_freshness",
        passed=True,
        detail=f"Runner image built {age_days:.1f} days ago (within limit)",
        evidence=f"build_ts={build_ts}, age_seconds={age_seconds:.0f}",
    )


def check_runner_checksum(
    metadata: Dict[str, str],
) -> ProvenanceResult:
    """Validate the runner-provided checksum over the image metadata.

    The checksum is a SHA-256 hash of canonical image metadata fields,
    allowing detection of tampered metadata.
    """
    runner_checksum = metadata.get("runner_checksum", "").strip()
    if not runner_checksum:
        return ProvenanceResult(
            check="runner_checksum",
            passed=not STRICT_MODE,
            detail="No runner checksum provided; skipping integrity check",
            evidence="",
        )

    # Compute expected checksum from core metadata fields
    fields = [
        metadata.get("runner_name", ""),
        metadata.get("image_name", ""),
        metadata.get("image_digest", ""),
        metadata.get("image_build_timestamp", ""),
    ]
    canonical = "|".join(fields)
    expected = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    if runner_checksum == expected:
        return ProvenanceResult(
            check="runner_checksum",
            passed=True,
            detail="Runner checksum matches computed image metadata hash",
            evidence=f"checksum={runner_checksum}",
        )

    return ProvenanceResult(
        check="runner_checksum",
        passed=False,
        detail="Runner checksum does not match computed metadata hash",
        evidence=f"got={runner_checksum}, expected={expected}",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_valid_digest_format(digest: str) -> bool:
    """Check if a digest string looks like a valid content-addressable hash."""
    if not digest:
        return False
    # Must be algo:hex format (sha256:, sha512:, etc.)
    if ":" not in digest:
        return False
    algo, _, hex_part = digest.partition(":")
    if not algo or not hex_part:
        return False
    # Hex part must be at least 32 chars (SHA-256 produces 64 hex chars)
    if len(hex_part) < 32:
        return False
    try:
        int(hex_part, 16)
    except ValueError:
        return False
    return True


def _record_metrics(results: List[ProvenanceResult]) -> None:
    """Record provenance check results via the metrics collector if available."""
    try:
        from src.common.metrics import metrics
        for result in results:
            status = "passed" if result.passed else "failed"
            metrics.increment(f"provenance.checks.{status}")
            metrics.increment(f"provenance.check.{result.check}.{'pass' if result.passed else 'fail'}")
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------

def validate_runner_provenance(
    metadata: Optional[Dict[str, str]] = None,
) -> ProvenanceReport:
    """Run all runner image provenance checks and return a report.

    Args:
        metadata: Runner metadata dict. If None, extracted from environment.

    Returns:
        ProvenanceReport with overall pass/fail and per-check results.
    """
    if metadata is None:
        metadata = extract_runner_metadata()

    runner_name = metadata.get("runner_name", "unknown")

    checks: List[Tuple[str, Callable]] = [
        ("approved_runner_labels", check_runner_labels),
        ("approved_image_digest", check_image_digest),
        ("image_build_freshness", check_image_freshness),
        ("runner_checksum", check_runner_checksum),
    ]

    results: List[ProvenanceResult] = []
    for check_name, check_fn in checks:
        try:
            result = check_fn(metadata)
        except Exception as exc:
            result = ProvenanceResult(
                check=check_name,
                passed=False,
                detail=f"Check raised exception: {exc}",
            )
        results.append(result)

    overall_passed = all(r.passed for r in results)

    # Record metrics when available
    _record_metrics(results)

    return ProvenanceReport(
        overall_passed=overall_passed,
        runner_name=runner_name,
        results=results,
    )


def format_report_for_summary(report: ProvenanceReport) -> str:
    """Format a provenance report as a GitHub step summary (Markdown)."""
    status_icon = "✅" if report.overall_passed else "❌"
    lines = [
        f"## Runner Provenance Validation {status_icon}",
        "",
        f"**Runner:** `{report.runner_name}`",
        f"**Checked at:** {report.checked_at}",
        f"**Overall:** {'PASSED' if report.overall_passed else 'FAILED'}",
        "",
        "| Check | Result | Detail |",
        "|-------|--------|--------|",
    ]
    for r in report.results:
        icon = "✅" if r.passed else "❌"
        lines.append(
            f"| {r.check} | {icon} | {r.detail} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Run provenance validation and write results.

    Exit code 0: all checks passed.
    Exit code 1: one or more checks failed.
    """
    report = validate_runner_provenance()

    # Write to GitHub step summary if available
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a") as f:
                f.write(format_report_for_summary(report))
                f.write("\n")
        except OSError:
            print("Warning: could not write to GITHUB_STEP_SUMMARY", file=sys.stderr)

    # Always print to stdout
    print(format_report_for_summary(report))

    if not report.overall_passed:
        failed_checks = [
            r.check for r in report.results if not r.passed
        ]
        print(
            f"\n❌ Provenance validation FAILED. "
            f"Failed checks: {', '.join(failed_checks)}",
            file=sys.stderr,
        )
        return 1

    print("\n✅ Runner provenance validated successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
