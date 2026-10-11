import re
from pathlib import Path

import pytest

from settings_samples import DEFAULTS, SAMPLES, UA_KEY
from subnet_tracker.core.settings import (
    SETTING_KEYS,
    ApplyStrategy,
    BrowserRefreshMode,
    SettingError,
    Settings,
    SettingsError,
    SettingState,
    SettingValue,
    StoredSetting,
    check_key,
    parse_setting,
    setting_state,
    setting_states,
    settings_from,
)
from subnet_tracker.types import PREFIX_V4, PREFIX_V6, ReconnectMode, Timestamp


def design_keys() -> list[str]:
    design = (Path(__file__).parents[1] / "docs" / "design.md").read_text(encoding="utf-8")
    section = design[design.index("\n### 13.2 ") : design.index("\n## 14. ")]
    first_cells = re.findall(r"^\| (`[^|]*`) \|", section, re.MULTILINE)
    return [key for cell in first_cells for key in re.findall(r"`([a-z]+\.[a-z0-9_]+)`", cell)]


def test_keys_follow_design_table() -> None:
    assert list(SETTING_KEYS) == design_keys()


def test_www_pair_setting_is_gone() -> None:
    assert "domains.www_pair" not in SETTING_KEYS


def test_samples_cover_every_key() -> None:
    assert SAMPLES.keys() == set(SETTING_KEYS)
    assert DEFAULTS.keys() == set(SETTING_KEYS) - {UA_KEY}


def test_defaults_without_stored_values() -> None:
    states = setting_states({})
    assert [s.key for s in states] == list(SETTING_KEYS)
    assert {s.key: s.value for s in states if s.key != UA_KEY} == DEFAULTS
    assert all(s.value == s.default and not s.changed and s.updated_at is None for s in states)


def test_default_user_agent_is_a_browser() -> None:
    ua = Settings().discovery.user_agent
    assert ua.startswith("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    assert "Chrome/" in ua


@pytest.mark.parametrize("key", SETTING_KEYS)
def test_default_passes_own_validation(key: str) -> None:
    state = setting_state(key, None)
    assert isinstance(state, SettingState)
    value = parse_setting(key, state.default)
    assert isinstance(value, SettingValue)
    assert value.text == state.default


def test_typed_defaults() -> None:
    s = settings_from({})
    assert s == Settings()
    assert s.resolve.interval == 3600
    assert s.resolve.unhealthy_ratio == 0.5
    assert s.browser.refresh_mode is BrowserRefreshMode.AUTO
    assert s.browser.max_age == 86400
    assert s.agent.poll_interval == 60
    assert s.routes.push_ipv4 is True
    assert s.routes.exclude == ()
    assert s.apply.strategy is ApplyStrategy.RECONNECT
    assert s.apply.after_add is ReconnectMode.ON_LEAVE
    assert s.discovery.max_bytes == 5 * 1024 * 1024
    assert s.discovery.ignore == ("w3.org", "schema.org", "ogp.me")
    assert s.history.retention == 90 * 86400


@pytest.mark.parametrize("key", SETTING_KEYS)
def test_parse_sample(key: str) -> None:
    text, canonical = SAMPLES[key]
    value = parse_setting(key, text)
    assert isinstance(value, SettingValue)
    assert (value.key, value.text) == (key, canonical)
    assert parse_setting(key, canonical) == value


@pytest.mark.parametrize(
    ("key", "text", "canonical"),
    [
        ("resolve.interval", "120m", "2h"),
        ("resolve.interval", "1s", "1s"),
        ("resolve.unhealthy_ratio", "1", "1.0"),
        ("resolve.unhealthy_ratio", "0.01", "0.01"),
        ("resolve.concurrency", "1024", "1024"),
        ("resolve.concurrency", "1", "1"),
        ("browser.refresh_mode", "off", "off"),
        ("apply.after_add", "auto", "auto"),
        ("routes.max_v4", "100000", "100000"),
        ("routes.exclude", "", ""),
        ("routes.exclude", " , ", ""),
        ("routes.exclude", "2001:db8::1", "2001:db8::1/128"),
        ("discovery.ignore", "", ""),
        ("discovery.ignore", "W3.ORG. w3.org。 example.com.", "w3.org, example.com"),
        ("discovery.max_bytes", "1024KB", "1MB"),
        ("discovery.max_bytes", "1B", "1B"),
        ("routes.retention", "0s", "0s"),
        ("history.retention", "0d", "0s"),
    ],
)
def test_parse_canonical(key: str, text: str, canonical: str) -> None:
    value = parse_setting(key, text)
    assert isinstance(value, SettingValue)
    assert value.text == canonical


@pytest.mark.parametrize(
    ("key", "text", "message"),
    [
        ("resolve.interval", "0s", "at least 1s"),
        ("resolve.interval", "1h30m", "invalid duration '1h30m'"),
        ("resolve.timeout", "3", "invalid duration '3'"),
        ("resolve.timeout", "", "invalid duration ''"),
        ("agent.poll_interval", "0m", "at least 1s"),
        ("agent.timeout", "0s", "at least 1s"),
        ("discovery.timeout", "0s", "at least 1s"),
        ("history.retention", "-1d", "invalid duration '-1d'"),
        ("resolve.concurrency", "0", "between 1 and 1024"),
        ("resolve.concurrency", "1025", "between 1 and 1024"),
        ("browser.concurrency", "0", "between 1 and 1024"),
        ("agent.concurrency", "2000", "between 1 and 1024"),
        ("resolve.concurrency", "abc", "invalid integer 'abc'"),
        ("resolve.concurrency", " 32", "invalid integer ' 32'"),
        ("resolve.concurrency", "+32", "invalid integer '+32'"),
        ("resolve.concurrency", "3_2", "invalid integer '3_2'"),
        ("resolve.concurrency", "32.0", "invalid integer '32.0'"),
        ("browser.batch", "0", "at least 1"),
        ("agent.batch", "-5", "at least 1"),
        ("routes.max_v4", "0", "at least 1"),
        ("routes.max_v6", "0", "at least 1"),
        ("resolve.unhealthy_ratio", "0", "greater than 0 and at most 1"),
        ("resolve.unhealthy_ratio", "1.5", "greater than 0 and at most 1"),
        ("resolve.unhealthy_ratio", "nan", "invalid number 'nan'"),
        ("resolve.unhealthy_ratio", "inf", "invalid number 'inf'"),
        ("resolve.unhealthy_ratio", "1e-1", "invalid number '1e-1'"),
        ("resolve.unhealthy_ratio", "half", "invalid number 'half'"),
        ("routes.push_ipv4", "yes", "expected true or false, got 'yes'"),
        ("routes.push_ipv6", "True", "expected true or false, got 'True'"),
        ("browser.refresh_mode", "always", "expected one of auto, button, off, got 'always'"),
        ("apply.strategy", "auto", "expected one of reconnect, none, got 'auto'"),
        ("apply.after_add", "none", "expected one of on_leave, auto, now, got 'none'"),
        ("apply.after_add", "ON_LEAVE", "expected one of on_leave, auto, now, got 'ON_LEAVE'"),
        ("discovery.max_bytes", "0B", "at least 1B"),
        ("discovery.max_bytes", "5 MB", "invalid size '5 MB'"),
        ("discovery.max_bytes", "5", "invalid size '5'"),
        ("routes.exclude", "10.0.0.1/8", "invalid network '10.0.0.1/8'"),
        ("routes.exclude", "10.0.0.0/8 nonsense", "invalid network 'nonsense'"),
        ("routes.exclude", "10.0.0.0/33", "invalid network '10.0.0.0/33'"),
        ("routes.exclude", "fe80::1%eth0", "invalid network 'fe80::1%eth0': zone index is not routable"),
        ("routes.exclude", "10.0.0.0/8 fe80::%eth0/64", "invalid network 'fe80::%eth0/64': zone index is not routable"),
        ("routes.exclude", "fe80::1%eth0/64", "invalid network 'fe80::1%eth0/64'"),
        ("discovery.ignore", "w3.org bad_domain!", "invalid domain name 'bad_domain!'"),
        ("discovery.ignore", "a..b", "invalid domain name 'a..b'"),
        ("discovery.ignore", "-a.com", "invalid domain name '-a.com'"),
        ("discovery.ignore", "localhost", "invalid domain name 'localhost': a single label is not a domain name"),
        ("discovery.ignore", "example.123", "invalid domain name 'example.123': numeric top-level label"),
        ("discovery.user_agent", "", "1 to 512 printable ASCII characters"),
        ("discovery.user_agent", "a\r\nX-Injected: 1", "1 to 512 printable ASCII characters"),
        ("discovery.user_agent", "агент", "1 to 512 printable ASCII characters"),
        ("discovery.user_agent", "a" * 513, "1 to 512 printable ASCII characters"),
    ],
)
def test_parse_rejects(key: str, text: str, message: str) -> None:
    error = parse_setting(key, text)
    assert isinstance(error, SettingError)
    assert error.key == key
    assert message in error.message


def test_unknown_key_rejected() -> None:
    error = SettingError("domains.www_pair", "unknown setting 'domains.www_pair'")
    assert parse_setting("domains.www_pair", "all") == error
    assert setting_state("domains.www_pair", None) == error
    assert check_key("domains.www_pair") == error
    assert check_key("resolve.interval") is None


def test_settings_from_stored_values() -> None:
    stored = {"resolve.concurrency": StoredSetting("64", Timestamp(5)), "domains.www_pair": StoredSetting('"all"', Timestamp(5))}
    assert settings_from(stored).resolve.concurrency == 64
    assert setting_state("resolve.concurrency", stored["resolve.concurrency"]) == SettingState("resolve.concurrency", "64", "32", Timestamp(5))
    assert "domains.www_pair" not in {s.key for s in setting_states(stored)}


def test_invalid_stored_value_names_the_key() -> None:
    stored = {"resolve.interval": StoredSetting('"0s"', Timestamp(0))}
    for read in (lambda: settings_from(stored), lambda: setting_states(stored), lambda: setting_state("resolve.interval", stored["resolve.interval"])):
        with pytest.raises(SettingsError, match=r"resolve\.interval.*must be at least 1s"):
            read()


@pytest.mark.parametrize(
    ("key", "limits"), [("routes.default_prefix_v4", PREFIX_V4), ("routes.min_aggregate_prefix_v4", PREFIX_V4), ("routes.default_prefix_v6", PREFIX_V6), ("routes.min_aggregate_prefix_v6", PREFIX_V6)]
)
def test_prefix_limits_match_schema(key: str, limits: range) -> None:
    low, high = limits[0], limits[-1]
    assert (low, high) == ((8, 32) if key.endswith("v4") else (16, 128))
    for inside in (low, high):
        assert isinstance(parse_setting(key, str(inside)), SettingValue)
    for outside in (low - 1, high + 1):
        assert parse_setting(key, str(outside)) == SettingError(key, f"must be between {low} and {high}")
