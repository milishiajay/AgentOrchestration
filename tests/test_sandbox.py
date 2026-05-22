import pytest
import os
import stat
from src.agent.sandbox import AgentSandbox

class TestAgentSandbox:
    def test_create_sandbox_directory(self):
        sandbox = AgentSandbox()
        path = sandbox.create("test_agent")
        assert path.exists()
        assert path.is_dir()
        sandbox.cleanup_all()

    def test_destroy_sandbox(self):
        sandbox = AgentSandbox()
        path = sandbox.create("test_agent")
        assert sandbox.destroy("test_agent")
        assert not path.exists()

    def test_get_path(self):
        sandbox = AgentSandbox()
        path = sandbox.create("test_agent")
        retrieved = sandbox.get_path("test_agent")
        assert retrieved == path
        sandbox.cleanup_all()

    def test_directory_permissions_restrictive(self):
        sandbox = AgentSandbox()
        path = sandbox.create("test_agent")
        mode = path.stat().st_mode & 0o777
        assert mode == 0o700, (
            f"Sandbox directory must have owner-only permissions (0o700), got {oct(mode)}"
        )
        sandbox.cleanup_all()
