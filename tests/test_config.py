import pytest
from src.common.config import Config
from src.common.errors import ConfigurationError


class TestConfig:
    def test_load_config(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text('{"app": {"name": "test", "port": 8080}}')
        config = Config(str(config_file))
        assert config.get("app.name") == "test"
        assert config.get("app.port") == 8080

    def test_default_value(self):
        config = Config()
        assert config.get("nonexistent.key", "default") == "default"

    def test_set_value(self):
        config = Config()
        config.set("database.host", "localhost")
        assert config.get("database.host") == "localhost"

    def test_nested_set(self):
        config = Config()
        config.set("a.b.c.d", "value")
        assert config.get("a.b.c.d") == "value"

    def test_to_dict(self):
        config = Config()
        config.set("key1", "value1")
        config.set("key2", "value2")
        data = config.to_dict()
        assert data["key1"] == "value1"
        assert data["key2"] == "value2"


class TestConfigGetInt:
    """Regression tests for Config.get_int — issue #503."""

    # ── happy path ─────────────────────────────────────────────────

    def test_json_integer(self):
        """JSON integer values are returned as-is."""
        config = Config()
        config.set("max_workers", 12)
        assert config.get_int("max_workers") == 12
        assert isinstance(config.get_int("max_workers"), int)

    def test_json_integer_zero(self):
        """Zero is a valid integer limit."""
        config = Config()
        config.set("max_workers", 0)
        assert config.get_int("max_workers") == 0

    def test_json_integer_negative(self):
        """Negative integers are passed through."""
        config = Config()
        config.set("offset", -5)
        assert config.get_int("offset") == -5

    def test_json_integer_large(self):
        """Large integers are preserved exactly."""
        config = Config()
        config.set("big", 2 ** 63 - 1)
        assert config.get_int("big") == 2 ** 63 - 1

    # ── string coercion ────────────────────────────────────────────

    def test_numeric_string(self):
        """String values that look like integers are coerced."""
        config = Config()
        config.set("timeout", "30")
        assert config.get_int("timeout") == 30
        assert isinstance(config.get_int("timeout"), int)

    def test_numeric_string_negative(self):
        """Negative numeric strings are coerced."""
        config = Config()
        config.set("offset", "-10")
        assert config.get_int("offset") == -10

    def test_numeric_string_with_whitespace(self):
        """Whitespace around the numeric string is tolerated."""
        config = Config()
        config.set("limit", "   42   ")
        assert config.get_int("limit") == 42

    # ── default handling ───────────────────────────────────────────

    def test_default_int(self):
        """When the key is missing, the supplied int default is returned."""
        config = Config()
        assert config.get_int("missing", 100) == 100

    def test_default_none(self):
        """When the key is missing and default is None, None is returned."""
        config = Config()
        assert config.get_int("missing") is None

    def test_default_zero(self):
        """Zero default is returned as-is (not ambiguous with None)."""
        config = Config()
        assert config.get_int("missing", 0) == 0

    # ── boolean rejection ──────────────────────────────────────────

    def test_boolean_true_rejected(self):
        """True must not be silently coerced to 1."""
        config = Config()
        config.set("flag", True)
        with pytest.raises(ConfigurationError, match="booleans are not valid"):
            config.get_int("flag")

    def test_boolean_false_rejected(self):
        """False must not be silently coerced to 0."""
        config = Config()
        config.set("flag", False)
        with pytest.raises(ConfigurationError, match="booleans are not valid"):
            config.get_int("flag")

    def test_boolean_default_rejected(self):
        """A bool default is a programming error and should be flagged."""
        config = Config()
        with pytest.raises(ConfigurationError, match="default must be an int"):
            config.get_int("missing", True)

    # ── rejection of non-integer defaults ──────────────────────────

    def test_float_default_rejected(self):
        """Non-integer default raises ConfigurationError."""
        config = Config()
        with pytest.raises(ConfigurationError, match="default must be an int"):
            config.get_int("missing", 3.14)

    def test_str_default_rejected(self):
        """String default raises ConfigurationError."""
        config = Config()
        with pytest.raises(ConfigurationError, match="default must be an int"):
            config.get_int("missing", "hi")

    # ── invalid stored values ──────────────────────────────────────

    def test_invalid_string(self):
        """A non-numeric string raises ConfigurationError."""
        config = Config()
        config.set("limit", "abc")
        with pytest.raises(ConfigurationError, match="cannot coerce"):
            config.get_int("limit")

    def test_float_non_whole(self):
        """A float with fractional part raises ConfigurationError."""
        config = Config()
        config.set("limit", 3.14)
        with pytest.raises(
            ConfigurationError, match="float value.*not a whole"
        ):
            config.get_int("limit")

    def test_float_whole(self):
        """A float that is a whole number is accepted (3.0 → 3)."""
        config = Config()
        config.set("limit", 3.0)
        assert config.get_int("limit") == 3

    def test_list_rejected(self):
        """A list value raises ConfigurationError."""
        config = Config()
        config.set("limit", [1, 2, 3])
        with pytest.raises(ConfigurationError, match="expected int"):
            config.get_int("limit")

    def test_dict_rejected(self):
        """A dict value raises ConfigurationError."""
        config = Config()
        config.set("limit", {"a": 1})
        with pytest.raises(ConfigurationError, match="expected int"):
            config.get_int("limit")

    def test_none_stored_value(self):
        """None stored as a value returns the default."""
        config = Config()
        config.set("limit", None)
        assert config.get_int("limit", 42) == 42

    # ── env override integration ───────────────────────────────────

    def test_env_override_coerces_numeric_string(self, monkeypatch):
        """AO_ env overrides are stored as strings and must be coerced."""
        monkeypatch.setenv("AO_LIMITS_MAX_WORKERS", "16")
        config = Config()
        # AO_LIMITS_MAX_WORKERS → limits.max.workers (all _ become .)
        assert config.get_int("limits.max.workers") == 16

    def test_env_override_invalid_string_raises(self, monkeypatch):
        """Invalid AO_ env overrides produce clear errors."""
        monkeypatch.setenv("AO_LIMITS_MAX_WORKERS", "notanumber")
        config = Config()
        with pytest.raises(ConfigurationError, match="cannot coerce"):
            config.get_int("limits.max.workers")

    # ── type correctness ──────────────────────────────────────────

    def test_return_type_is_int(self):
        """Successful calls always return int (or None)."""
        config = Config()
        config.set("a", 5)
        assert isinstance(config.get_int("a"), int)

    def test_dotted_path_resolution(self):
        """get_int uses the same dotted-path resolution as get."""
        config = Config()
        config.set("nested.deep.key", 77)
        assert config.get_int("nested.deep.key") == 77

# 2019-02-01T18:58:35 update

# 2019-07-31T13:45:15 update

# 2019-08-09T17:54:41 update

# 2019-08-14T16:29:54 update

# 2019-10-11T10:28:34 update

# 2019-10-25T09:23:55 update

# 2019-12-13T09:04:47 update

# 2020-04-09T10:21:21 update

# 2020-05-08T17:44:24 update

# 2020-07-20T13:54:19 update

# 2020-09-24T15:42:29 update

# 2020-12-09T20:16:24 update

# 2021-04-21T13:19:36 update

# 2021-05-25T09:15:06 update

# 2021-10-13T20:37:29 update

# 2021-11-18T18:37:15 update

# 2021-12-05T14:46:27 update

# 2022-01-19T12:56:31 update

# 2022-03-03T14:31:21 update

# 2022-03-23T08:42:05 update

# 2022-03-23T16:05:36 update

# 2022-07-11T19:00:31 update

# 2022-11-23T12:37:19 update

# 2023-01-16T15:28:31 update

# 2023-02-10T11:37:41 update

# 2023-08-01T09:43:10 update

# 2023-08-25T11:04:56 update

# 2023-09-07T10:18:27 update

# 2023-10-03T08:52:54 update

# 2023-10-11T19:49:55 update

# 2023-12-04T09:53:42 update

# 2024-01-29T14:34:37 update

# 2024-03-27T08:22:58 update

# 2024-07-03T09:52:12 update

# 2024-07-18T12:14:11 update

# 2024-09-12T10:59:12 update

# 2024-09-16T15:56:14 update

# 2024-09-17T19:00:45 update

# 2024-09-25T08:04:43 update

# 2024-12-10T14:49:57 update

# 2024-12-31T08:27:41 update

# 2025-03-18T15:08:24 update

# 2025-05-13T18:23:05 update

# 2025-05-15T19:05:40 update

# 2025-06-09T15:01:44 update

# 2025-07-04T18:13:41 update

# 2025-07-23T15:44:03 update

# 2025-10-16T13:53:26 update

# 2025-11-12T18:42:00 update

# 2026-02-06T08:55:54 update

# 2026-02-11T19:28:37 update

# 2026-04-17T10:00:53 update
