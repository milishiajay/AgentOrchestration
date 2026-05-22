import time

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.api.templates import Principal, template_clone_service


class TestTemplateCloneAuth:
    def setup_method(self):
        template_clone_service.reset()
        template_clone_service.register_template(
            "workspace-1",
            "template-1",
            {"agent_type": "worker.processor"},
        )
        template_clone_service.register_principal(
            "good-token",
            Principal(
                principal_id="user-1",
                workspace_id="workspace-1",
                role="editor",
                scopes=["templates:clone"],
            ),
        )
        self.client = TestClient(create_app())

    # ------------------------------------------------------------------ #
    #  Authorized paths — bearer, session, owner, admin                  #
    # ------------------------------------------------------------------ #

    def test_authorized_editor_can_clone_template(self):
        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer good-token"},
            json={"name": "copied-template"},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "copied-template"
        assert template_clone_service.template_read_count == 1
        assert template_clone_service.clone_mutation_count == 1
        assert template_clone_service.audit_events[-1]["decision"] == (
            "clone_created"
        )

    def test_authorized_owner_can_clone_template(self):
        template_clone_service.register_principal(
            "owner-token",
            Principal(
                principal_id="user-owner",
                workspace_id="workspace-1",
                role="owner",
                scopes=["templates:clone"],
            ),
        )
        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer owner-token"},
            json={"name": "owner-copy"},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "owner-copy"
        assert template_clone_service.template_read_count == 1
        assert template_clone_service.clone_mutation_count == 1

    def test_authorized_admin_can_clone_template(self):
        template_clone_service.register_principal(
            "admin-token",
            Principal(
                principal_id="user-admin",
                workspace_id="workspace-1",
                role="admin",
                scopes=["templates:clone"],
            ),
        )
        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer admin-token"},
            json={"name": "admin-copy"},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "admin-copy"
        assert template_clone_service.template_read_count == 1
        assert template_clone_service.clone_mutation_count == 1

    def test_authorized_browser_session_can_clone_template(self):
        self.client.cookies.set("ao_session", "good-token")

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            json={"name": "session-copy"},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "session-copy"
        assert template_clone_service.template_read_count == 1
        assert template_clone_service.clone_mutation_count == 1
        assert template_clone_service.audit_events[-1]["decision"] == (
            "clone_created"
        )

    # ------------------------------------------------------------------ #
    #  Deny paths — anonymous, malformed, unknown, revoked, disabled,    #
    #  expired (bearer + session), wrong workspace, missing scope,       #
    #  insufficient role, stale                                          #
    # ------------------------------------------------------------------ #

    def test_no_authorization_header_fails_before_template_read_or_mutation(
        self,
    ):
        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            json={"name": "copy"},
        )

        # AuthMiddleware rejects before reaching the service layer
        assert response.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0

    def test_malformed_auth_header_fails_before_read_or_mutation(
        self,
    ):
        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Token good-token"},
            json={"name": "copy"},
        )

        # AuthMiddleware rejects malformed auth before service layer
        assert response.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0

    def test_unknown_token_fails_before_template_read_or_mutation(self):
        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer missing-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-1]["decision"] == (
            "unknown_token"
        )

    def test_revoked_principal_fails_before_template_read_or_mutation(self):
        template_clone_service.register_principal(
            "revoked-token",
            Principal(
                principal_id="user-2",
                workspace_id="workspace-1",
                role="admin",
                scopes=["templates:clone"],
                revoked=True,
            ),
        )

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer revoked-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-1]["decision"] == (
            "revoked_principal"
        )
        assert "revoked-token" not in template_clone_service.principals

    def test_revoked_principal_fails_closed_on_repeat_use(self):
        template_clone_service.register_principal(
            "revoked-token",
            Principal(
                principal_id="user-2",
                workspace_id="workspace-1",
                role="admin",
                scopes=["templates:clone"],
                revoked=True,
            ),
        )

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer revoked-token"},
            json={"name": "copy"},
        )
        repeat = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer revoked-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 401
        assert repeat.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-2]["decision"] == (
            "revoked_principal"
        )
        assert template_clone_service.audit_events[-1]["decision"] == (
            "unknown_token"
        )
        assert "revoked-token" not in template_clone_service.principals

    def test_disabled_principal_fails_before_template_read_or_mutation(self):
        template_clone_service.register_principal(
            "disabled-token",
            Principal(
                principal_id="user-disabled",
                workspace_id="workspace-1",
                role="admin",
                scopes=["templates:clone"],
                disabled=True,
            ),
        )

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer disabled-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-1]["decision"] == (
            "disabled_principal"
        )
        assert "disabled-token" not in template_clone_service.principals

    def test_disabled_principal_fails_closed_on_repeat_use(self):
        template_clone_service.register_principal(
            "disabled-token",
            Principal(
                principal_id="user-disabled",
                workspace_id="workspace-1",
                role="admin",
                scopes=["templates:clone"],
                disabled=True,
            ),
        )

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer disabled-token"},
            json={"name": "copy"},
        )
        repeat = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer disabled-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 401
        assert repeat.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-2]["decision"] == (
            "disabled_principal"
        )
        assert template_clone_service.audit_events[-1]["decision"] == (
            "unknown_token"
        )
        assert "disabled-token" not in template_clone_service.principals

    def test_expired_principal_fails_before_template_read_or_mutation(self):
        template_clone_service.register_principal(
            "expired-token",
            Principal(
                principal_id="user-3",
                workspace_id="workspace-1",
                role="admin",
                scopes=["templates:clone"],
                expires_at=time.time() - 1,
            ),
        )

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer expired-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-1]["decision"] == (
            "expired_principal"
        )
        assert "expired-token" not in template_clone_service.principals

    def test_expired_browser_session_fails_closed_and_is_invalidated(self):
        template_clone_service.register_principal(
            "expired-session",
            Principal(
                principal_id="user-7",
                workspace_id="workspace-1",
                role="admin",
                scopes=["templates:clone"],
                expires_at=time.time() - 1,
            ),
        )
        self.client.cookies.set("ao_session", "expired-session")

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            json={"name": "copy"},
        )
        repeat = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            json={"name": "copy"},
        )

        assert response.status_code == 401
        assert repeat.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-2]["decision"] == (
            "expired_principal"
        )
        assert template_clone_service.audit_events[-1]["decision"] == (
            "unknown_token"
        )
        assert "expired-session" not in template_clone_service.principals

    def test_wrong_workspace_fails_before_template_read_or_mutation(self):
        template_clone_service.register_principal(
            "wrong-workspace-token",
            Principal(
                principal_id="user-4",
                workspace_id="workspace-2",
                role="admin",
                scopes=["templates:clone"],
            ),
        )

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer wrong-workspace-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 403
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-1]["decision"] == (
            "wrong_workspace"
        )

    def test_missing_scope_fails_before_template_read_or_mutation(self):
        template_clone_service.register_principal(
            "missing-scope-token",
            Principal(
                principal_id="user-5",
                workspace_id="workspace-1",
                role="admin",
                scopes=["templates:read"],
            ),
        )

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer missing-scope-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 403
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-1]["decision"] == (
            "missing_scope"
        )

    def test_viewer_role_fails_before_template_read_or_mutation(self):
        template_clone_service.register_principal(
            "viewer-token",
            Principal(
                principal_id="user-6",
                workspace_id="workspace-1",
                role="viewer",
                scopes=["templates:clone"],
            ),
        )

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer viewer-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 403
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-1]["decision"] == (
            "insufficient_role"
        )

    def test_blank_browser_session_fails_before_read_or_mutation(self):
        self.client.cookies.set("ao_session", " ")

        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            json={"name": "copy"},
        )

        assert response.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-1]["decision"] == (
            "missing_auth"
        )

    # ------------------------------------------------------------------ #
    #  Scope isolation and boundary tests                                #
    # ------------------------------------------------------------------ #

    def test_browser_session_cookie_does_not_bypass_other_api_auth(self):
        self.client.cookies.set("ao_session", "good-token")

        response = self.client.get("/api/v2/agents")

        assert response.status_code == 401
        assert template_clone_service.template_read_count == 0
        assert template_clone_service.clone_mutation_count == 0

    def test_authorized_principal_cannot_clone_nonexistent_template(self):
        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/nonexistent/clone",
            headers={"Authorization": "Bearer good-token"},
            json={"name": "copy"},
        )

        assert response.status_code == 404
        assert template_clone_service.template_read_count == 1
        assert template_clone_service.clone_mutation_count == 0
        assert template_clone_service.audit_events[-1]["decision"] == (
            "missing_template"
        )

    def test_clone_without_payload_uses_default_name(self):
        response = self.client.post(
            "/api/v2/workspaces/workspace-1/templates/template-1/clone",
            headers={"Authorization": "Bearer good-token"},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "template-1-clone"
        assert template_clone_service.template_read_count == 1
        assert template_clone_service.clone_mutation_count == 1
