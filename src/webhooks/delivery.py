"""Webhook delivery with secret-redacted failure logging.

Implements the delivery-log lifecycle required by #590:
- Inbound webhook payloads are delivered to target endpoints.
- Failures are logged with sanitised representations — secrets/tokens
  are replaced with [REDACTED] before storage.
- Retry/replay metadata (attempt count, HTTP status, timestamp) is preserved.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.webhooks.redact import SecretRedactor, REDACTED
from src.common.metrics import metrics


@dataclass
class WebhookDeliveryLog:
    """Sanitised delivery failure record.

    Secrets are redacted *before* construction; this dataclass never
    sees a raw token.  Retry/replay information is fully preserved.
    """

    delivery_id: str
    webhook_id: str
    endpoint_url: str
    event_type: str
    status_code: Optional[int]
    attempt: int = 1
    error_message: str = ""
    sanitized_headers: Dict[str, str] = field(default_factory=dict)
    sanitized_body: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "webhook_id": self.webhook_id,
            "endpoint_url": self.endpoint_url,
            "event_type": self.event_type,
            "status": "failed",
            "status_code": self.status_code,
            "attempt": self.attempt,
            "error_message": self.error_message,
            "sanitized_headers": self.sanitized_headers,
            "sanitized_body": self.sanitized_body,
            "timestamp": self.timestamp,
        }


class WebhookDelivery:
    """Webhook delivery engine with secret redaction on failure.

    Failures are stored as sanitised delivery logs.  Raw payloads and
    secrets are never persisted; retry/replay metadata is fully retained.
    """

    def __init__(
        self,
        max_retries: int = 3,
        secret_patterns: Optional[List[str]] = None,
    ) -> None:
        self.max_retries = max_retries
        self.redactor = SecretRedactor(extra_patterns=secret_patterns)
        self.successful: List[Dict[str, Any]] = []
        self.failed_logs: List[WebhookDeliveryLog] = []
        self.delivery_history: List[Dict[str, Any]] = []

    # ── Public API ──────────────────────────────────────────────────────

    def deliver(
        self,
        webhook_id: str,
        endpoint_url: str,
        event_type: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Deliver a webhook and return outcome.

        On failure, stores a sanitised delivery log — no secrets leak.
        Returns a summary dict suitable for API responses.
        """
        delivery_id = str(uuid.uuid4())
        headers = dict(headers or {})

        # Simulate delivery (real implementation would use HTTP client)
        status_code: Optional[int] = None
        error_msg: str = ""
        for attempt in range(1, self.max_retries + 1):
            success, status_code, error_msg = self._simulate_delivery(
                endpoint_url, payload, headers, attempt, self.max_retries
            )
            if success:
                record = {
                    "delivery_id": delivery_id,
                    "webhook_id": webhook_id,
                    "status": "delivered",
                    "attempt": attempt,
                    "status_code": status_code,
                }
                self.successful.append(record)
                self.delivery_history.append(record)
                metrics.increment("webhook.delivery.success")
                return record

        # All attempts exhausted — log sanitised failure
        sanitized_headers = self.redactor.redact_dict(headers)
        sanitized_body = self.redactor.redact_dict(payload)
        log = WebhookDeliveryLog(
            delivery_id=delivery_id,
            webhook_id=webhook_id,
            endpoint_url=endpoint_url,
            event_type=event_type,
            status_code=status_code,
            attempt=self.max_retries,
            error_message=error_msg,
            sanitized_headers=sanitized_headers,
            sanitized_body=sanitized_body,
        )
        self.failed_logs.append(log)
        self.delivery_history.append(log.to_dict())
        metrics.increment("webhook.delivery.failure")
        return {
            "delivery_id": delivery_id,
            "webhook_id": webhook_id,
            "status": "failed",
            "attempt": self.max_retries,
            "error_message": error_msg,
            "status_code": status_code,
        }

    def replay(
        self,
        webhook_id: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Replay a previously failed webhook delivery.

        Preserves retry semantics.  Stores replay attempt in delivery
        history with a replay flag so audit trails can distinguish
        initial delivery from retries.
        """
        headers = dict(headers or {})
        status_code: Optional[int] = None
        error_msg: str = ""
        for attempt in range(1, self.max_retries + 1):
            success, status_code, error_msg = self._simulate_delivery(
                "", payload, headers, attempt, self.max_retries
            )
            if success:
                record = {
                    "delivery_id": str(uuid.uuid4()),
                    "webhook_id": webhook_id,
                    "status": "replayed",
                    "attempt": attempt,
                    "status_code": status_code,
                    "replay": True,
                }
                self.successful.append(record)
                self.delivery_history.append(record)
                return record

        # Replay failed — sanitised log
        sanitized_headers = self.redactor.redact_dict(headers)
        sanitized_body = self.redactor.redact_dict(payload)
        delivery_id = str(uuid.uuid4())
        log = WebhookDeliveryLog(
            delivery_id=delivery_id,
            webhook_id=webhook_id,
            endpoint_url="(replay)",
            event_type="webhook_replay",
            status_code=status_code,
            attempt=self.max_retries,
            error_message=error_msg,
            sanitized_headers=sanitized_headers,
            sanitized_body=sanitized_body,
        )
        self.failed_logs.append(log)
        self.delivery_history.append(log.to_dict())
        return {
            "delivery_id": delivery_id,
            "webhook_id": webhook_id,
            "status": "replay_failed",
            "attempt": self.max_retries,
            "error_message": error_msg,
            "status_code": status_code,
        }

    def get_failed_logs(self) -> List[Dict[str, Any]]:
        """Return all sanitised failure logs."""
        return [log.to_dict() for log in self.failed_logs]

    def get_delivery_history(self) -> List[Dict[str, Any]]:
        """Return complete delivery history (successes + sanitised failures)."""
        return list(self.delivery_history)

    # ── Internal ────────────────────────────────────────────────────────

    @staticmethod
    def _simulate_delivery(
        endpoint_url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        attempt: int,
        max_retries: int,
    ) -> tuple:
        """Simulate an HTTP delivery.

        In a real system this would use httpx/aiohttp.  The simulation
        fails for certain payload markers to exercise the retry path.
        """
        # Simulate transient failures that resolve on retry
        if payload.get("simulate_retry_success") and attempt < max_retries:
            return False, 503, "Service Unavailable (simulated)"
        # Simulate permanent failure
        if payload.get("simulate_permanent_failure"):
            return False, 500, "Internal Server Error (simulated)"
        # Simulate auth failure
        if payload.get("simulate_unauthorized"):
            return False, 401, "Unauthorized (simulated)"
        return True, 200, ""
