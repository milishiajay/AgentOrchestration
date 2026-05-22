"""Secret redaction engine for webhook failure logs.

Implements configurable pattern-based redaction that replaces secrets,
tokens, API keys, and other sensitive values with [REDACTED] before
persistence in delivery logs.

Two-pronged approach:
1. Key-name matching: values under known secret keys are always redacted.
2. Value-pattern matching: string values matching secret patterns are redacted.

Follows the same sanitised-audit pattern used across the platform:
retention, registry handler resolution, and template clone audit.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Pattern

# Sentinel value used as redaction placeholder
REDACTED = "[REDACTED]"

# Key names whose values should always be redacted (case-insensitive)
_SECRET_KEYS: List[str] = [
    "api_key", "apikey", "secret_key", "secret", "access_key",
    "auth_token", "token", "bearer_token", "password", "passwd",
    "authorization", "private_key", "pat", "credentials",
]

# Patterns that identify secret values irrespective of key name
_DEFAULT_VALUE_PATTERNS: List[str] = [
    # Key-based secrets: key=value, key: value, "key": "value" (JSON)
    r'(?i)(api[_-]?key|apikey|secret[_-]?key|access[_-]?key|auth[_-]?token|password|passwd|bearer_token)[=:]\s*["\']?[^"\s,}]+["\']?',
    r'(?i)"(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?key|auth[_-]?token|password|passwd|bearer_token|token|pat|authorization|secret)"\s*:\s*"[^"]{4,}"',
    # Bearer tokens
    r"Bearer\s+\S+",
    # Token=value forms
    r"token[=:]\s*\S+",
    # OpenAI-style API keys
    r'sk-[a-zA-Z0-9]{16,}',
    # GitHub PAT
    r'ghp_[a-zA-Z0-9]{36}',
    # Slack tokens
    r'xox[bprs]-[a-zA-Z0-9-]+',
    # PEM-encoded private keys
    r'-----BEGIN\s+(?:RSA|EC|DSA|OPENSSH|PGP)\s+PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA|EC|DSA|OPENSSH|PGP)\s+PRIVATE\s+KEY-----',
]


class SecretRedactor:
    """Configurable secret redaction engine.

    Redacts secrets using two strategies:
    1. Known secret-key names — any value under such a key is fully redacted.
    2. Value-pattern matching — regex patterns applied to string values.
    """

    def __init__(
        self,
        patterns: Optional[List[str]] = None,
        extra_patterns: Optional[List[str]] = None,
        secret_keys: Optional[List[str]] = None,
    ) -> None:
        self.secret_keys: set = set(k.lower() for k in (secret_keys or _SECRET_KEYS))

        if patterns is not None:
            source = list(patterns)
        else:
            source = list(_DEFAULT_VALUE_PATTERNS)
        if extra_patterns:
            source.extend(extra_patterns)
        self._compiled: List[Pattern] = [
            re.compile(p, re.IGNORECASE | re.DOTALL) for p in source
        ]

    def redact(self, value: str) -> str:
        """Redact secrets from a plain string using value patterns."""
        for pattern in self._compiled:
            value = pattern.sub(REDACTED, value)
        return value

    def redact_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-redact all values in a dictionary.

        Values under known secret keys are fully replaced with REDACTED.
        Other string values are checked against secret-value patterns.
        Nested dicts and lists are traversed recursively.
        """
        result: Dict[str, Any] = {}
        for key, value in data.items():
            result[key] = self._redact_value(key, value)
        return result

    def _redact_value(self, key: str, value: Any) -> Any:
        """Redact a single value based on its key and content."""
        # Check if the key itself signals a secret
        if key.lower() in self.secret_keys:
            if isinstance(value, str) and value:
                return REDACTED
            if isinstance(value, (dict, list)):
                # Redact entire subtree under a secret key
                return REDACTED
            if value is not None:
                return REDACTED
            return value

        # Apply value-pattern matching to strings
        if isinstance(value, str):
            redacted = self.redact(value)
            if redacted != value:
                return redacted
            return value

        if isinstance(value, dict):
            return self.redact_dict(value)

        if isinstance(value, list):
            return [self._redact_value("", v) for v in value]

        return value
