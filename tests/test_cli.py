"""Tests for CLI commands, including deploy dry-run mode (#535)."""

import subprocess
import sys

import pytest


def run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Run the ao CLI with given args and return the result."""
    return subprocess.run(
        [sys.executable, "-m", "src.cli.main"] + args,
        capture_output=True,
        text=True,
        timeout=10,
    )


class TestDeployCommand:
    """Tests for the 'deploy' subcommand."""

    def test_deploy_normal_mode(self, tmp_path):
        """Normal deploy prints a deploying message."""
        manifest = tmp_path / "agent.yaml"
        manifest.write_text("name: test-agent\nversion: 1.0\n")
        result = run_cli(["deploy", str(manifest)])
        assert result.returncode == 0
        assert "Deploying agent from manifest" in result.stdout

    def test_deploy_dry_run_flag_accepted(self, tmp_path):
        """--dry-run flag is accepted and does not actually deploy."""
        manifest = tmp_path / "agent.yaml"
        manifest.write_text("name: test-agent\nversion: 1.0\n")
        result = run_cli(["deploy", "--dry-run", str(manifest)])
        assert result.returncode == 0

    def test_dry_run_shows_preview_message(self, tmp_path):
        """Dry-run output indicates no actual deployment occurred."""
        manifest = tmp_path / "agent.yaml"
        manifest.write_text("name: test-agent\nversion: 1.0\n")
        result = run_cli(["deploy", "--dry-run", str(manifest)])
        assert "[DRY RUN]" in result.stdout
        assert "No actual deployment was performed" in result.stdout

    def test_dry_run_includes_manifest_path(self, tmp_path):
        """Dry-run output includes the manifest path."""
        manifest = tmp_path / "agent.yaml"
        manifest.write_text("name: test-agent\nversion: 1.0\n")
        result = run_cli(["deploy", "--dry-run", str(manifest)])
        assert "Manifest path" in result.stdout
        assert str(manifest) in result.stdout

    def test_dry_run_does_not_print_deploying(self, tmp_path):
        """Dry-run output should NOT contain the standard deploy message."""
        manifest = tmp_path / "agent.yaml"
        manifest.write_text("name: test-agent\nversion: 1.0\n")
        result = run_cli(["deploy", "--dry-run", str(manifest)])
        assert "Deploying agent from manifest" not in result.stdout

    def test_normal_deploy_does_not_show_dry_run(self, tmp_path):
        """Normal deploy should NOT contain any dry-run messages."""
        manifest = tmp_path / "agent.yaml"
        manifest.write_text("name: test-agent\nversion: 1.0\n")
        result = run_cli(["deploy", str(manifest)])
        assert "[DRY RUN]" not in result.stdout
        assert "No actual deployment was performed" not in result.stdout

    def test_dry_run_exits_zero(self, tmp_path):
        """Dry-run should exit successfully (exit code 0)."""
        manifest = tmp_path / "agent.yaml"
        manifest.write_text("name: test-agent\nversion: 1.0\n")
        result = run_cli(["deploy", "--dry-run", str(manifest)])
        assert result.returncode == 0

    def test_deploy_without_manifest_shows_usage(self):
        """Deploy without a manifest argument prints usage and exits non-zero."""
        result = run_cli(["deploy"])
        assert result.returncode != 0

    def test_deploy_help_includes_dry_run(self):
        """deploy --help should mention --dry-run flag."""
        result = run_cli(["deploy", "--help"])
        assert "--dry-run" in result.stdout

    def test_deploy_missing_manifest_exits_nonzero(self):
        """Deploy with a non-existent manifest should fail before printing progress."""
        result = run_cli(["deploy", "/nonexistent/path/manifest.yaml"])
        assert result.returncode != 0
        assert "manifest file not found" in result.stderr

    def test_deploy_missing_manifest_no_progress_message(self):
        """Deploy with a missing manifest must NOT print 'Deploying' or '[DRY RUN]'."""
        result = run_cli(["deploy", "/nonexistent/path/manifest.yaml"])
        assert "Deploying agent from manifest" not in result.stdout
        assert "[DRY RUN]" not in result.stdout
