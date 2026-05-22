"""Regression tests for webhook secret redaction — #590.

Covers:
- Secrets redacted from failure delivery logs
- Retry/replay metadata preserved after redaction
- Token patterns (Bearer, API key, GitHub PAT, Slack tokens)
- Deep nesting: secrets in nested dicts and lists are redacted
- Successful deliveries do not leak secrets in history records
- Replay preserves redaction on failure
- Custom redactor patterns
"""

import pytest
from src.webhooks.delivery import WebhookDelivery, WebhookDeliveryLog
from src.webhooks.redact import SecretRedactor, REDACTED


class TestSecretRedactor:
    """Unit tests for the redaction engine."""

    def setup_method(self):
        self.redactor = SecretRedactor()

    # ── Plain string redaction ────────────────────────────────────────

    def test_redact_bearer_token_in_header(self):
        value = "Bearer fake-sk-testing-only-not-real"
        result = self.redactor.redact(value)
        assert REDACTED in result
        assert "fake-sk-testing-only-not-real" not in result

    def test_redact_api_key_in_string(self):
        value = '{"api_key": "super-secret-value-12345", "name": "test"}'
        result = self.redactor.redact(value)
        assert REDACTED in result
        assert "super-secret-value-12345" not in result

    def test_redact_github_pat(self):
        value = "ghp_fakepat1234567890abcdef1234567890abc"
        result = self.redactor.redact(value)
        assert REDACTED in result
        assert "ghp_fakepat" not in result

    def test_redact_slack_token(self):
        value = "xoxb-fake1234567890-notarealtoken"
        result = self.redactor.redact(value)
        assert REDACTED in result

    def test_redact_token_key_value(self):
        value = "token=abc123secret456"
        result = self.redactor.redact(value)
        assert REDACTED in result

    def test_non_secret_string_passes_through(self):
        value = "This is a normal log message"
        result = self.redactor.redact(value)
        assert result == value

    # ── Dict redaction ────────────────────────────────────────────────

    def test_redact_dict_top_level(self):
        data = {
            "event": "task.created",
            "headers": {
                "Authorization": "Bearer sk-secret-key-123",
                "Content-Type": "application/json",
            },
        }
        result = self.redactor.redact_dict(data)
        assert result["event"] == "task.created"
        assert result["headers"]["Content-Type"] == "application/json"
        assert REDACTED in result["headers"]["Authorization"]
        assert "sk-secret-key-123" not in result["headers"]["Authorization"]

    def test_redact_dict_nested(self):
        data = {
            "task": {
                "config": {
                    "api_key": "deeply-nested-secret",
                    "endpoint": "https://api.example.com",
                }
            }
        }
        result = self.redactor.redact_dict(data)
        assert result["task"]["config"]["endpoint"] == "https://api.example.com"
        assert REDACTED in result["task"]["config"]["api_key"]
        assert "deeply-nested-secret" not in str(result)

    def test_redact_list_inside_dict(self):
        data = {
            "tokens": ["Bearer abc", "Bearer xyz", "normal"],
        }
        result = self.redactor.redact_dict(data)
        assert result["tokens"][0] == REDACTED
        assert result["tokens"][1] == REDACTED
        assert result["tokens"][2] == "normal"

    # ── Custom patterns ───────────────────────────────────────────────

    def test_custom_pattern(self):
        redactor = SecretRedactor(extra_patterns=[r"custom_secret_\d+"])
        result = redactor.redact("Use custom_secret_42 here")
        assert REDACTED in result
        assert "custom_secret_42" not in result

    def test_replace_all_defaults(self):
        redactor = SecretRedactor(patterns=[r"my-pattern-\d+"])
        result = redactor.redact("Bearer token and my-pattern-1")
        # Default Bearer pattern was removed; custom pattern still matches
        assert REDACTED in result
        assert "my-pattern-1" not in result
        # Bearer is NOT redacted because defaults were replaced
        assert "Bearer" in result


class TestWebhookDelivery:
    """Tests for webhook delivery with secret redaction on failure."""

    def setup_method(self):
        self.delivery = WebhookDelivery(max_retries=3)

    # ── Failure log redaction ─────────────────────────────────────────

    def test_secrets_redacted_from_failure_log(self):
        """Secrets in payload and headers are redacted from failure logs."""
        result = self.delivery.deliver(
            webhook_id="wh-1",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.completed",
            payload={
                "task_id": "task-1",
                "api_key": "super-secret-key-12345",
                "auth_token": "Bearer abc-def-ghi",
                "simulate_permanent_failure": True,
            },
            headers={
                "Authorization": "Bearer sk-sensitive-data",
                "X-Webhook-ID": "wh-1",
            },
        )
        assert result["status"] == "failed"

        logs = self.delivery.get_failed_logs()
        assert len(logs) == 1
        log = logs[0]

        # Retry/replay metadata preserved
        assert log["attempt"] == 3
        assert log["webhook_id"] == "wh-1"
        assert log["event_type"] == "task.completed"
        assert log["endpoint_url"] == "https://hooks.example.com/webhook"
        assert log["status_code"] is not None

        # No raw secrets in sanitized body
        body_str = str(log["sanitized_body"])
        assert "super-secret-key-12345" not in body_str
        assert "abc-def-ghi" not in body_str
        assert "sk-sensitive-data" not in body_str

        # REDACTED sentinel present where secrets were
        header_str = str(log["sanitized_headers"])
        assert REDACTED in header_str
        assert REDACTED in body_str

    def test_delivery_log_dict_contains_no_raw_secrets(self):
        """The to_dict() output must not contain raw tokens anywhere."""
        result = self.delivery.deliver(
            webhook_id="wh-2",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={
                "task_id": "task-2",
                "secret_key": "ghp_aaaa1111bbbb2222cccc3333dddd4444eeee",
                "simulate_permanent_failure": True,
            },
        )
        assert result["status"] == "failed"

        log_dict = self.delivery.get_failed_logs()[0]
        flat = str(log_dict)
        assert "ghp_aaaa1111bbbb2222cccc3333dddd4444eeee" not in flat
        assert REDACTED in flat

    # ── Successful delivery does not expose secrets ────────────────────

    def test_successful_delivery_no_raw_secrets_in_history(self):
        """History records for successful deliveries must not leak payload."""
        result = self.delivery.deliver(
            webhook_id="wh-3",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={
                "task_id": "task-3",
                "api_key": "should-not-appear",
            },
        )
        assert result["status"] == "delivered"

        history = self.delivery.get_delivery_history()
        # Success records are summary dicts, not full delivery logs
        success_record = history[0]
        assert "api_key" not in str(success_record)
        assert "should-not-appear" not in str(success_record)

    def test_successful_deliveries_not_in_failed_logs(self):
        """Successful deliveries are tracked separately from failure logs."""
        self.delivery.deliver(
            webhook_id="wh-success",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={"task_id": "ok"},
        )
        assert len(self.delivery.successful) == 1
        assert len(self.delivery.failed_logs) == 0

    # ── Retry / replay metadata ────────────────────────────────────────

    def test_retry_metadata_preserved_in_failure_log(self):
        """Retry count and error info survive redaction."""
        self.delivery.deliver(
            webhook_id="wh-retry",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.completed",
            payload={
                "task_id": "rt-1",
                "secret": "token-abc",
                "simulate_permanent_failure": True,
            },
        )
        log = self.delivery.get_failed_logs()[0]
        assert log["attempt"] == 3
        assert log["error_message"]
        assert log["status_code"] is not None
        assert log["timestamp"] > 0

    def test_transient_failure_retries_succeed(self):
        """A transient failure that resolves on retry succeeds."""
        result = self.delivery.deliver(
            webhook_id="wh-transient",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={
                "task_id": "task-t",
                "simulate_retry_success": True,
                "token": "Bearer abc123",
            },
        )
        assert result["status"] == "delivered"
        assert result["attempt"] == 3  # resolves on last retry

    def test_permanent_failure_generates_sanitized_log(self):
        """A permanent failure logs sanitized info with status code."""
        result = self.delivery.deliver(
            webhook_id="wh-perm",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={
                "simulate_permanent_failure": True,
                "api_key": "perm-secret-999",
            },
        )
        assert result["status"] == "failed"
        log = self.delivery.get_failed_logs()[0]
        assert log["status_code"] == 500
        assert "perm-secret-999" not in str(log)

    def test_unauthorized_failure_redacts(self):
        """401 response secrets are redacted from the log."""
        result = self.delivery.deliver(
            webhook_id="wh-unauth",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={
                "simulate_unauthorized": True,
                "bearer_token": "Bearer bad-token-here",
            },
        )
        assert result["status"] == "failed"
        log = self.delivery.get_failed_logs()[0]
        assert log["status_code"] == 401
        assert "bad-token-here" not in str(log["sanitized_body"])

    # ── Replay ─────────────────────────────────────────────────────────

    def test_replay_failure_redacts_secrets(self):
        """Replay failures produce sanitized logs."""
        result = self.delivery.replay(
            webhook_id="wh-replay",
            payload={
                "simulate_permanent_failure": True,
                "secret_key": "replay-secret-abc",
            },
            headers={"Authorization": "Bearer replay-token"},
        )
        assert result["status"] == "replay_failed"

        logs = self.delivery.get_failed_logs()
        assert len(logs) == 1
        log = logs[0]
        assert log["webhook_id"] == "wh-replay"
        assert log["event_type"] == "webhook_replay"
        flat = str(log)
        assert "replay-secret-abc" not in flat
        assert "replay-token" not in flat

    def test_replay_success_preserves_replay_flag(self):
        """Successful replay records carry a replay flag."""
        result = self.delivery.replay(
            webhook_id="wh-replay-ok",
            payload={"task_id": "ok"},
        )
        assert result["status"] == "replayed"
        assert result["replay"] is True

    # ── Delivery history ───────────────────────────────────────────────

    def test_delivery_history_tracks_all_outcomes(self):
        """get_delivery_history returns both successes and sanitised failures."""
        self.delivery.deliver(
            webhook_id="wh-ok",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={"task_id": "ok-task"},
        )
        self.delivery.deliver(
            webhook_id="wh-fail",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={"simulate_permanent_failure": True},
        )
        history = self.delivery.get_delivery_history()
        assert len(history) == 2
        assert history[0]["status"] == "delivered"
        assert history[1]["status"] == "failed"

    # ── Deep nesting edge cases ────────────────────────────────────────

    def test_deeply_nested_secrets_redacted(self):
        """Secrets in deeply nested payloads are fully redacted."""
        self.delivery.deliver(
            webhook_id="wh-deep",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="config.updated",
            payload={
                "simulate_permanent_failure": True,
                "workspace": {
                    "settings": {
                        "integrations": [
                            {
                                "name": "slack",
                                "token": "xoxb-deep-nested-token",
                            },
                            {
                                "name": "github",
                                "pat": "ghp_deeplyhidden1234567890abcdef1234567890abcdef",
                            },
                        ]
                    }
                },
            },
        )
        log = self.delivery.get_failed_logs()[0]
        flat = str(log["sanitized_body"])
        assert "xoxb-deep-nested-token" not in flat
        assert "ghp_deeplyhidden" not in flat
        assert REDACTED in flat

    def test_empty_payload_produces_empty_sanitized_body(self):
        """Empty payloads (minus sim flags) produce clean sanitized body."""
        self.delivery.deliver(
            webhook_id="wh-empty",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={"simulate_permanent_failure": True},
        )
        log = self.delivery.get_failed_logs()[0]
        # Simulation flags are non-secret metadata that survive redaction
        assert log["sanitized_body"] == {"simulate_permanent_failure": True}

    def test_numeric_and_bool_values_preserved(self):
        """Non-string values survive redaction unchanged."""
        self.delivery.deliver(
            webhook_id="wh-types",
            endpoint_url="https://hooks.example.com/webhook",
            event_type="task.created",
            payload={
                "count": 42,
                "enabled": True,
                "ratio": 3.14,
                "secret": "Bearer should-be-redacted",
                "simulate_permanent_failure": True,
            },
        )
        log = self.delivery.get_failed_logs()[0]
        body = log["sanitized_body"]
        assert body["count"] == 42
        assert body["enabled"] is True
        assert body["ratio"] == 3.14
        assert REDACTED in body["secret"]
