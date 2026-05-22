"""Tests for runner image provenance validation.

Covers:
- valid provenance (all checks pass)
- unapproved runner labels
- unapproved / invalid image digest
- stale / unparseable build timestamp
- missing allowlist entry (strict mode)
- checksum mismatch
- strict vs non-strict mode behavior
- CLI exit codes
- summary format
"""

import os
import sys
import tempfile
from unittest.mock import patch

import pytest

# Ensure the scripts directory is on the path
SCRIPT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "scripts"
)
sys.path.insert(0, SCRIPT_DIR)

# Import validation module directly
import validate_runner_provenance as validate_module

# Aliases for convenience
validate_runner_provenance = validate_module.validate_runner_provenance
ProvenanceReport = validate_module.ProvenanceReport
ProvenanceResult = validate_module.ProvenanceResult
check_runner_labels = validate_module.check_runner_labels
check_image_digest = validate_module.check_image_digest
check_image_freshness = validate_module.check_image_freshness
check_runner_checksum = validate_module.check_runner_checksum
extract_runner_metadata = validate_module.extract_runner_metadata
format_report_for_summary = validate_module.format_report_for_summary

# Also import the agent provenance module
from src.agent.provenance import (
    runner_provenance_preflight,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APPROVED_LABELS_METADATA = {
    "runner_name": "ci-runner-01",
    "runner_labels": "self-hosted,linux,x64,ubuntu-22.04",
    "image_os": "ubuntu-22.04",
    "image_name": "ubuntu-22.04",
    "image_digest": "sha256:abc123def4567890abcdef1234567890abcdef1234567890abcdef1234567890",
    "image_build_timestamp": "2026-05-15T12:00:00Z",
    "runner_checksum": (
        "sha256:"
        + __import__("hashlib").sha256(
            "ci-runner-01|ubuntu-22.04|"
            "sha256:abc123def4567890abcdef1234567890abcdef1234567890abcdef1234567890|"
            "2026-05-15T12:00:00Z".encode()
        ).hexdigest()
    ),
}


# ---------------------------------------------------------------------------
# Test: valid provenance
# ---------------------------------------------------------------------------

class TestValidProvenance:
    """All checks should pass with valid, approved metadata."""

    def test_all_checks_pass(self):
        report = validate_runner_provenance(APPROVED_LABELS_METADATA)
        assert report.overall_passed
        assert len(report.results) == 4
        for r in report.results:
            assert r.passed, f"{r.check} should pass: {r.detail}"

    def test_runner_name_in_report(self):
        report = validate_runner_provenance(APPROVED_LABELS_METADATA)
        assert report.runner_name == "ci-runner-01"

    def test_checked_at_timestamp(self):
        report = validate_runner_provenance(APPROVED_LABELS_METADATA)
        assert report.checked_at  # non-empty ISO timestamp


class TestRunnerLabels:
    """Validate approved runner labels."""

    def test_approved_labels_pass(self):
        result = check_runner_labels(APPROVED_LABELS_METADATA)
        assert result.passed
        assert "approved" in result.detail.lower()

    def test_unapproved_labels_fail(self):
        meta = dict(APPROVED_LABELS_METADATA)
        meta["runner_labels"] = "self-hosted,linux,bad-label"
        result = check_runner_labels(meta)
        assert not result.passed
        assert "bad-label" in result.detail

    def test_empty_labels_graceful(self):
        meta = dict(APPROVED_LABELS_METADATA)
        meta["runner_labels"] = ""
        result = check_runner_labels(meta)
        # Non-strict: passes with warning
        assert result.passed

    def test_missing_labels_graceful(self):
        meta = dict(APPROVED_LABELS_METADATA)
        del meta["runner_labels"]
        result = check_runner_labels(meta)
        assert result.passed  # non-strict default


class TestImageDigest:
    """Validate image digest against allowlist."""

    def test_matching_digest_passes(self):
        result = check_image_digest(APPROVED_LABELS_METADATA)
        assert result.passed
        assert "matches" in result.detail.lower()

    def test_mismatched_digest_fails(self):
        meta = dict(APPROVED_LABELS_METADATA)
        meta["image_digest"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        result = check_image_digest(meta)
        assert not result.passed
        assert "not match" in result.detail.lower()

    def test_missing_digest_passes_non_strict(self):
        meta = dict(APPROVED_LABELS_METADATA)
        del meta["image_digest"]
        result = check_image_digest(meta)
        assert result.passed  # non-strict: allowed

    def test_invalid_digest_format_fails(self):
        meta = dict(APPROVED_LABELS_METADATA)
        meta["image_digest"] = "not-a-digest"
        result = check_image_digest(meta)
        assert not result.passed
        assert "invalid" in result.detail.lower()

    def test_digest_no_name_but_valid_format_passes_non_strict(self):
        meta = dict(APPROVED_LABELS_METADATA)
        meta["image_name"] = ""
        meta["image_digest"] = "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        result = check_image_digest(meta)
        assert result.passed  # valid format, non-strict, no allowlist entry


class TestImageFreshness:
    """Validate image build freshness."""

    def test_recent_build_passes(self):
        result = check_image_freshness(APPROVED_LABELS_METADATA)
        assert result.passed

    def test_very_old_build_fails(self):
        meta = dict(APPROVED_LABELS_METADATA)
        meta["image_build_timestamp"] = "2020-01-01T00:00:00Z"
        result = check_image_freshness(meta)
        assert not result.passed
        assert "old" in result.detail.lower()

    def test_missing_timestamp_passes_non_strict(self):
        meta = dict(APPROVED_LABELS_METADATA)
        del meta["image_build_timestamp"]
        result = check_image_freshness(meta)
        assert result.passed

    def test_invalid_timestamp_fails(self):
        meta = dict(APPROVED_LABELS_METADATA)
        meta["image_build_timestamp"] = "garbage-date"
        result = check_image_freshness(meta)
        assert not result.passed
        assert "parse" in result.detail.lower()


class TestRunnerChecksum:
    """Validate runner-provided checksum over metadata."""

    def test_matching_checksum_passes(self):
        result = check_runner_checksum(APPROVED_LABELS_METADATA)
        assert result.passed

    def test_mismatched_checksum_fails(self):
        meta = dict(APPROVED_LABELS_METADATA)
        meta["runner_checksum"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        result = check_runner_checksum(meta)
        assert not result.passed
        assert "not match" in result.detail.lower()

    def test_missing_checksum_passes_non_strict(self):
        meta = dict(APPROVED_LABELS_METADATA)
        del meta["runner_checksum"]
        result = check_runner_checksum(meta)
        assert result.passed


class TestStrictMode:
    """Strict mode: missing metadata should fail."""

    def test_strict_mode_missing_digest_fails(self):
        with patch.object(
            validate_module, "STRICT_MODE", True
        ):
            meta = dict(APPROVED_LABELS_METADATA)
            del meta["image_digest"]
            result = check_image_digest(meta)
            assert not result.passed

    def test_strict_mode_missing_labels_fails(self):
        with patch.object(
            validate_module, "STRICT_MODE", True
        ):
            meta = dict(APPROVED_LABELS_METADATA)
            meta["runner_labels"] = ""
            result = check_runner_labels(meta)
            assert not result.passed

    def test_strict_mode_overall_report_fails(self):
        with patch.object(
            validate_module, "STRICT_MODE", True
        ):
            meta = dict(APPROVED_LABELS_METADATA)
            del meta["image_digest"]
            del meta["image_build_timestamp"]
            report = validate_runner_provenance(meta)
            assert not report.overall_passed


class TestSummaryFormatting:
    """Ensure provenance report is properly formatted for GITHUB_STEP_SUMMARY."""

    def test_summary_contains_status(self):
        report = validate_runner_provenance(APPROVED_LABELS_METADATA)
        summary = format_report_for_summary(report)
        assert "PASSED" in summary
        assert "ci-runner-01" in summary

    def test_summary_contains_all_checks(self):
        report = validate_runner_provenance(APPROVED_LABELS_METADATA)
        summary = format_report_for_summary(report)
        for r in report.results:
            assert r.check in summary

    def test_failed_summary_shows_properly(self):
        meta = dict(APPROVED_LABELS_METADATA)
        meta["runner_labels"] = "self-hosted,disallowed"
        report = validate_runner_provenance(meta)
        summary = format_report_for_summary(report)
        assert "FAILED" in summary


class TestCLI:
    """Test the CLI entry point."""

    def test_cli_exits_zero_on_success(self):
        with patch.dict(
            os.environ,
            {
                "RUNNER_NAME": "ci-test",
                "RUNNER_LABELS": "self-hosted,linux,x64",
                "AO_RUNNER_IMAGE_NAME": "ubuntu-22.04",
                "AO_RUNNER_IMAGE_DIGEST": "sha256:abc123def4567890abcdef1234567890abcdef1234567890abcdef1234567890",
                "AO_RUNNER_IMAGE_BUILD_TS": "2026-05-15T12:00:00Z",
                "AO_RUNNER_CHECKSUM": (
                    "sha256:"
                    + __import__("hashlib").sha256(
                        "ci-test|ubuntu-22.04|"
                        "sha256:abc123def4567890abcdef1234567890abcdef1234567890abcdef1234567890|"
                        "2026-05-15T12:00:00Z".encode()
                    ).hexdigest()
                ),
            },
            clear=True,
        ):
            exit_code = validate_module.main()
            assert exit_code == 0

    def test_cli_exits_one_on_failure(self):
        with patch.dict(
            os.environ,
            {
                "RUNNER_NAME": "ci-test",
                "RUNNER_LABELS": "disallowed",
                "AO_RUNNER_IMAGE_NAME": "",
                "AO_RUNNER_IMAGE_DIGEST": "",
                "AO_RUNNER_IMAGE_BUILD_TS": "",
                "AO_RUNNER_CHECKSUM": "",
            },
            clear=True,
        ):
            with patch.object(
                validate_module, "STRICT_MODE", True
            ):
                exit_code = validate_module.main()
                assert exit_code == 1

    def test_cli_writes_to_step_summary(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
            tf_path = tf.name

        try:
            with patch.dict(
                os.environ,
                {
                    "RUNNER_NAME": "ci-test",
                    "RUNNER_LABELS": "self-hosted,linux,x64",
                    "AO_RUNNER_IMAGE_NAME": "ubuntu-22.04",
                    "AO_RUNNER_IMAGE_DIGEST": "sha256:abc123def4567890abcdef1234567890abcdef1234567890abcdef1234567890",
                    "AO_RUNNER_IMAGE_BUILD_TS": "2026-05-15T12:00:00Z",
                    "AO_RUNNER_CHECKSUM": (
                        "sha256:"
                        + __import__("hashlib").sha256(
                            "ci-test|ubuntu-22.04|"
                            "sha256:abc123def4567890abcdef1234567890abcdef1234567890abcdef1234567890|"
                            "2026-05-15T12:00:00Z".encode()
                        ).hexdigest()
                    ),
                    "GITHUB_STEP_SUMMARY": tf_path,
                },
                clear=True,
            ):
                validate_module.main()

            with open(tf_path) as f:
                content = f.read()
            assert "Runner Provenance Validation" in content
            assert "PASSED" in content
        finally:
            os.unlink(tf_path)


class TestAgentProvenanceHook:
    """Test the runner_provenance_preflight hook for orchestrator integration."""

    def test_valid_provenance_hook_passes(self):
        with patch.dict(
            os.environ,
            {
                "RUNNER_NAME": "ci-test",
                "RUNNER_LABELS": "self-hosted,linux,x64",
                "AO_RUNNER_IMAGE_NAME": "ubuntu-22.04",
                "AO_RUNNER_IMAGE_DIGEST": "sha256:abc123def4567890abcdef1234567890abcdef1234567890abcdef1234567890",
                "AO_RUNNER_IMAGE_BUILD_TS": "2026-05-15T12:00:00Z",
            },
            clear=True,
        ):
            allowed, reason = runner_provenance_preflight()
            assert allowed
            assert "validated" in reason.lower()

    def test_unapproved_labels_hook_fails(self):
        with patch.dict(
            os.environ,
            {
                "RUNNER_NAME": "ci-test",
                "RUNNER_LABELS": "disallowed",
                "AO_RUNNER_IMAGE_DIGEST": "",
                "AO_RUNNER_IMAGE_BUILD_TS": "",
                "AO_RUNNER_CHECKSUM": "",
                "AO_RUNNER_STRICT": "1",
            },
            clear=True,
        ):
            allowed, reason = runner_provenance_preflight(strict=True)
            assert not allowed
            assert "provenance failed" in reason.lower()

    def test_strict_hook_overrides_env(self):
        """Explicit strict param overrides env setting."""
        with patch.dict(
            os.environ,
            {
                "RUNNER_NAME": "ci-test",
                "RUNNER_LABELS": "self-hosted,linux,x64",
                "AO_RUNNER_IMAGE_DIGEST": "",
                "AO_RUNNER_IMAGE_BUILD_TS": "",
                "AO_RUNNER_STRICT": "0",
            },
            clear=True,
        ):
            # Non-strict env, but strict=True passed explicitly
            allowed, reason = runner_provenance_preflight(strict=True)
            assert not allowed  # missing digest fails in strict

    def test_non_strict_hook_allows_missing_metadata(self):
        with patch.dict(
            os.environ,
            {
                "RUNNER_NAME": "ci-test",
                "RUNNER_LABELS": "self-hosted,linux,x64",
            },
            clear=True,
        ):
            allowed, reason = runner_provenance_preflight(strict=False)
            assert allowed


class TestMetadataExtraction:
    """Test environment metadata extraction."""

    def test_extracts_from_env(self):
        with patch.dict(
            os.environ,
            {
                "RUNNER_NAME": "my-runner",
                "RUNNER_LABELS": "self-hosted,linux",
                "AO_RUNNER_IMAGE_NAME": "ubuntu-22.04",
                "AO_RUNNER_IMAGE_DIGEST": "sha256:abc123",
                "AO_RUNNER_IMAGE_BUILD_TS": "2026-01-01T00:00:00Z",
                "AO_RUNNER_CHECKSUM": "sha256:def456",
            },
            clear=True,
        ):
            meta = extract_runner_metadata()
            assert meta["runner_name"] == "my-runner"
            assert meta["runner_labels"] == "self-hosted,linux"
            assert meta["image_digest"] == "sha256:abc123"


class TestDigestFormatValidation:
    """Test the digest format validator in isolation."""

    def test_valid_sha256(self):
        assert validate_module._is_valid_digest_format(
            "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )

    def test_valid_sha512(self):
        assert validate_module._is_valid_digest_format(
            "sha512:" + "a" * 128
        )

    def test_no_colon_invalid(self):
        assert not validate_module._is_valid_digest_format("abc123")

    def test_too_short_hex(self):
        assert not validate_module._is_valid_digest_format("sha256:abc")

    def test_non_hex_invalid(self):
        assert not validate_module._is_valid_digest_format("sha256:zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")

    def test_empty_invalid(self):
        assert not validate_module._is_valid_digest_format("")
        assert not validate_module._is_valid_digest_format("sha256:")
