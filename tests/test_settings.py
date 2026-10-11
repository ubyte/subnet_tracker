import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from ipaddress import ip_network
from pathlib import Path

import pytest

from db_rows import insert
from subnet_tracker.core.db import connect
from subnet_tracker.core.settings import (
    SETTING_KEYS,
    AgentSettings,
    ApplySettings,
    ApplyStrategy,
    BrowserRefreshMode,
    BrowserSettings,
    DiscoverySettings,
    HistorySettings,
    ResolverSettings,
    ResolveSettings,
    RoutesSettings,
    SettingError,
    Settings,
    SettingsError,
    SettingState,
    SettingValue,
    get_setting,
    list_settings,
    load_settings,
    parse_setting,
    reset_setting,
    set_setting,
)
from subnet_tracker.types import Bytes, ReconnectMode, Seconds, Timestamp

NOW = Timestamp(1_760_000_000)
UA_KEY = "discovery.user_agent"

DEFAULTS = {
    "resolve.interval": "1h",
    "resolve.timeout": "3s",
    "resolve.concurrency": "32",
    "resolve.unhealthy_ratio": "0.5",
    "resolvers.stale_warning": "2d",
    "browser.refresh_mode": "auto",
    "browser.max_age": "1d",
    "browser.batch": "300",
    "browser.concurrency": "6",
    "agent.max_age": "1h",
    "agent.batch": "200",
    "agent.poll_interval": "1m",
    "agent.concurrency": "8",
    "agent.timeout": "5s",
    "agent.offline_after": "30m",
    "routes.retention": "4d",
    "routes.default_prefix_v4": "23",
    "routes.default_prefix_v6": "48",
    "routes.push_ipv4": "true",
    "routes.push_ipv6": "true",
    "routes.exclude": "",
    "routes.max_v4": "1000",
    "routes.max_v6": "300",
    "routes.min_aggregate_prefix_v4": "12",
    "routes.min_aggregate_prefix_v6": "32",
    "apply.strategy": "reconnect",
    "apply.after_add": "on_leave",
    "apply.min_interval": "1h",
    "apply.idle_delay": "30s",
    "apply.leave_grace": "5s",
    "apply.reconnect_spread": "2s",
    "discovery.timeout": "10s",
    "discovery.max_bytes": "5MB",
    "discovery.ignore": "w3.org, schema.org, ogp.me",
    "history.retention": "90d",
}

# A non-default value for every key: (input text, canonical text).
SAMPLES = {
    "resolve.interval": ("90m", "90m"),
    "resolve.timeout": ("10s", "10s"),
    "resolve.concurrency": ("64", "64"),
    "resolve.unhealthy_ratio": ("0.25", "0.25"),
    "resolvers.stale_warning": ("72h", "3d"),
    "browser.refresh_mode": ("button", "button"),
    "browser.max_age": ("12h", "12h"),
    "browser.batch": ("100", "100"),
    "browser.concurrency": ("4", "4"),
    "agent.max_age": ("1800s", "30m"),
    "agent.batch": ("50", "50"),
    "agent.poll_interval": ("90s", "90s"),
    "agent.concurrency": ("16", "16"),
    "agent.timeout": ("2s", "2s"),
    "agent.offline_after": ("2h", "2h"),
    "routes.retention": ("7d", "7d"),
    "routes.default_prefix_v4": ("24", "24"),
    "routes.default_prefix_v6": ("56", "56"),
    "routes.push_ipv4": ("false", "false"),
    "routes.push_ipv6": ("false", "false"),
    "routes.exclude": ("10.0.0.0/8,2001:db8::/32  192.0.2.1 10.0.0.0/8", "10.0.0.0/8, 2001:db8::/32, 192.0.2.1/32"),
    "routes.max_v4": ("500", "500"),
    "routes.max_v6": ("100", "100"),
    "routes.min_aggregate_prefix_v4": ("16", "16"),
    "routes.min_aggregate_prefix_v6": ("40", "40"),
    "apply.strategy": ("none", "none"),
    "apply.after_add": ("now", "now"),
    "apply.min_interval": ("0s", "0s"),
    "apply.idle_delay": ("0s", "0s"),
    "apply.leave_grace": ("10s", "10s"),
    "apply.reconnect_spread": ("1s", "1s"),
    "discovery.timeout": ("20s", "20s"),
    "discovery.max_bytes": ("512KB", "512KB"),
    "discovery.user_agent": ("curl/8.0 (x; y)", "curl/8.0 (x; y)"),
    "discovery.ignore": ("Example.COM, пример.рф example.com", "example.com, xn--e1afmkfd.xn--p1ai"),
    "history.retention": ("30d", "30d"),
}

SAMPLE_SETTINGS = Settings(
    resolve=ResolveSettings(interval=Seconds(5400), timeout=Seconds(10), concurrency=64, unhealthy_ratio=0.25),
    resolvers=ResolverSettings(stale_warning=Seconds(3 * 86400)),
    browser=BrowserSettings(refresh_mode=BrowserRefreshMode.BUTTON, max_age=Seconds(12 * 3600), batch=100, concurrency=4),
    agent=AgentSettings(max_age=Seconds(1800), batch=50, poll_interval=Seconds(90), concurrency=16, timeout=Seconds(2), offline_after=Seconds(7200)),
    routes=RoutesSettings(
        retention=Seconds(7 * 86400),
        default_prefix_v4=24,
        default_prefix_v6=56,
        push_ipv4=False,
        push_ipv6=False,
        exclude=(ip_network("10.0.0.0/8"), ip_network("2001:db8::/32"), ip_network("192.0.2.1/32")),
        max_v4=500,
        max_v6=100,
        min_aggregate_prefix_v4=16,
        min_aggregate_prefix_v6=40,
    ),
    apply=ApplySettings(strategy=ApplyStrategy.NONE, after_add=ReconnectMode.NOW, min_interval=Seconds(0), idle_delay=Seconds(0), leave_grace=Seconds(10), reconnect_spread=Seconds(1)),
    discovery=DiscoverySettings(timeout=Seconds(20), max_bytes=Bytes(512 * 1024), user_agent="curl/8.0 (x; y)", ignore=("example.com", "xn--e1afmkfd.xn--p1ai")),
    history=HistorySettings(retention=Seconds(30 * 86400)),
)


def design_keys() -> list[str]:
    design = (Path(__file__).parents[1] / "docs" / "design.md").read_text(encoding="utf-8")
    section = design[design.index("\n### 13.2 ") : design.index("\n## 14. ")]
    first_cells = re.findall(r"^\| (`[^|]*`) \|", section, re.MULTILINE)
    return [key for cell in first_cells for key in re.findall(r"`([a-z]+\.[a-z0-9_]+)`", cell)]


def stored(db: sqlite3.Connection, key: str) -> object:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else json.loads(row["value"])


def store_raw(db: sqlite3.Connection, key: str, value: str) -> None:
    insert(db, "settings", {"key": key, "value": value, "updated_at": 0})


def test_keys_follow_design_table() -> None:
    assert list(SETTING_KEYS) == design_keys()


def test_www_pair_setting_is_gone() -> None:
    assert "domains.www_pair" not in SETTING_KEYS


def test_samples_cover_every_key() -> None:
    assert SAMPLES.keys() == set(SETTING_KEYS)
    assert DEFAULTS.keys() == set(SETTING_KEYS) - {UA_KEY}


def test_defaults_on_empty_db(db: sqlite3.Connection) -> None:
    states = list_settings(db)
    assert [s.key for s in states] == list(SETTING_KEYS)
    assert {s.key: s.value for s in states if s.key != UA_KEY} == DEFAULTS
    assert all(s.value == s.default and not s.changed and s.updated_at is None for s in states)


def test_default_user_agent_is_a_browser() -> None:
    ua = Settings().discovery.user_agent
    assert ua.startswith("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    assert "Chrome/" in ua


@pytest.mark.parametrize("key", SETTING_KEYS)
def test_default_passes_own_validation(db: sqlite3.Connection, key: str) -> None:
    state = get_setting(db, key)
    assert isinstance(state, SettingState)
    value = parse_setting(key, state.default)
    assert isinstance(value, SettingValue)
    assert value.text == state.default


def test_typed_defaults(db: sqlite3.Connection) -> None:
    s = load_settings(db)
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
        ("routes.default_prefix_v4", "32", "32"),
        ("routes.default_prefix_v4", "1", "1"),
        ("routes.default_prefix_v6", "128", "128"),
        ("routes.min_aggregate_prefix_v6", "1", "1"),
        ("routes.max_v4", "100000", "100000"),
        ("routes.exclude", "", ""),
        ("routes.exclude", " , ", ""),
        ("routes.exclude", "2001:db8::1", "2001:db8::1/128"),
        ("discovery.ignore", "", ""),
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
        ("routes.default_prefix_v4", "0", "between 1 and 32"),
        ("routes.default_prefix_v4", "33", "between 1 and 32"),
        ("routes.default_prefix_v6", "129", "between 1 and 128"),
        ("routes.min_aggregate_prefix_v4", "0", "between 1 and 32"),
        ("routes.min_aggregate_prefix_v6", "200", "between 1 and 128"),
        ("discovery.max_bytes", "0B", "at least 1B"),
        ("discovery.max_bytes", "5 MB", "invalid size '5 MB'"),
        ("discovery.max_bytes", "5", "invalid size '5'"),
        ("routes.exclude", "10.0.0.1/8", "invalid network '10.0.0.1/8'"),
        ("routes.exclude", "10.0.0.0/8 nonsense", "invalid network 'nonsense'"),
        ("routes.exclude", "10.0.0.0/33", "invalid network '10.0.0.0/33'"),
        ("discovery.ignore", "w3.org bad_domain!", "invalid domain name 'bad_domain!'"),
        ("discovery.ignore", "a..b", "invalid domain name 'a..b'"),
        ("discovery.ignore", "-a.com", "invalid domain name '-a.com'"),
        ("discovery.ignore", "example.com.", "invalid domain name 'example.com.'"),
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


def test_unknown_key_rejected(db: sqlite3.Connection) -> None:
    for error in (parse_setting("domains.www_pair", "all"), get_setting(db, "domains.www_pair"), set_setting(db, "domains.www_pair", "all", NOW), reset_setting(db, "domains.www_pair")):
        assert error == SettingError("domains.www_pair", "unknown setting 'domains.www_pair'")
    assert stored(db, "domains.www_pair") is None


@pytest.mark.parametrize("key", SETTING_KEYS)
def test_set_get_round_trip(db: sqlite3.Connection, key: str) -> None:
    text, canonical = SAMPLES[key]
    value = set_setting(db, key, text, NOW)
    assert isinstance(value, SettingValue)
    assert value.text == canonical
    state = get_setting(db, key)
    assert isinstance(state, SettingState)
    assert (state.value, state.updated_at, state.changed) == (canonical, NOW, True)
    assert state.default == next(s.default for s in list_settings(db) if s.key == key)


def test_load_every_sample(db: sqlite3.Connection) -> None:
    for key, (text, _) in SAMPLES.items():
        assert isinstance(set_setting(db, key, text, NOW), SettingValue)
    assert load_settings(db) == SAMPLE_SETTINGS
    assert all(s.changed for s in list_settings(db))


@pytest.mark.parametrize(
    ("key", "text", "json_value"),
    [
        ("resolve.interval", "90m", "90m"),
        ("resolve.interval", "120m", "2h"),
        ("resolve.concurrency", "64", 64),
        ("resolve.unhealthy_ratio", "0.25", 0.25),
        ("routes.push_ipv4", "false", False),
        ("apply.strategy", "none", "none"),
        ("discovery.max_bytes", "1024KB", "1MB"),
        ("routes.exclude", "10.0.0.0/8 10.0.0.0/8 ::/0", ["10.0.0.0/8", "::/0"]),
        ("discovery.ignore", "", []),
        ("discovery.user_agent", "curl/8.0", "curl/8.0"),
    ],
)
def test_stored_json(db: sqlite3.Connection, key: str, text: str, json_value: object) -> None:
    set_setting(db, key, text, NOW)
    assert stored(db, key) == json_value


def test_set_is_committed(db: sqlite3.Connection, tmp_path: Path) -> None:
    set_setting(db, "resolve.concurrency", "64", NOW)
    assert not db.in_transaction
    with closing(connect(tmp_path / "state.db")) as other:
        assert load_settings(other).resolve.concurrency == 64


def test_set_invalid_keeps_value(db: sqlite3.Connection) -> None:
    set_setting(db, "resolve.concurrency", "64", NOW)
    assert isinstance(set_setting(db, "resolve.concurrency", "0", Timestamp(NOW + 1)), SettingError)
    state = get_setting(db, "resolve.concurrency")
    assert isinstance(state, SettingState)
    assert (state.value, state.updated_at) == ("64", NOW)


def test_set_overwrites(db: sqlite3.Connection) -> None:
    set_setting(db, "resolve.concurrency", "64", NOW)
    set_setting(db, "resolve.concurrency", "16", Timestamp(NOW + 1))
    assert get_setting(db, "resolve.concurrency") == SettingState("resolve.concurrency", "16", "32", Timestamp(NOW + 1))


def test_set_default_value_removes_row(db: sqlite3.Connection) -> None:
    set_setting(db, "resolve.interval", "90m", NOW)
    value = set_setting(db, "resolve.interval", "60m", Timestamp(NOW + 1))
    assert isinstance(value, SettingValue)
    assert value.text == "1h"
    assert stored(db, "resolve.interval") is None
    assert get_setting(db, "resolve.interval") == SettingState("resolve.interval", "1h", "1h", None)


def test_reset(db: sqlite3.Connection) -> None:
    set_setting(db, "routes.exclude", "10.0.0.0/8", NOW)
    assert reset_setting(db, "routes.exclude") is None
    assert stored(db, "routes.exclude") is None
    assert load_settings(db).routes.exclude == ()
    assert reset_setting(db, "routes.exclude") is None
    assert not db.in_transaction


def test_changed_compares_with_default(db: sqlite3.Connection) -> None:
    store_raw(db, "resolve.interval", '"60m"')
    assert get_setting(db, "resolve.interval") == SettingState("resolve.interval", "1h", "1h", Timestamp(0))
    assert not SettingState("resolve.interval", "1h", "1h", Timestamp(0)).changed


def test_unknown_stored_key_ignored(db: sqlite3.Connection) -> None:
    store_raw(db, "domains.www_pair", '"all"')
    assert load_settings(db) == Settings()
    assert "domains.www_pair" not in {s.key for s in list_settings(db)}


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("resolve.interval", '"0s"'),
        ("resolve.interval", "3600"),
        ("resolve.interval", '"1h30m"'),
        ("resolve.concurrency", '"32"'),
        ("resolve.concurrency", "true"),
        ("resolve.concurrency", "32.0"),
        ("resolve.concurrency", "0"),
        ("resolve.unhealthy_ratio", "true"),
        ("resolve.unhealthy_ratio", '"0.5"'),
        ("resolve.unhealthy_ratio", "2"),
        ("routes.push_ipv4", "1"),
        ("routes.push_ipv4", '"true"'),
        ("apply.after_add", '"none"'),
        ("apply.after_add", "null"),
        ("discovery.max_bytes", "5242880"),
        ("routes.exclude", '"10.0.0.0/8"'),
        ("routes.exclude", "[1]"),
        ("routes.exclude", '["10.0.0.1/8"]'),
        ("discovery.ignore", '{"w3.org": true}'),
        ("discovery.user_agent", '""'),
    ],
)
def test_invalid_stored_value_is_an_error(db: sqlite3.Connection, key: str, raw: str) -> None:
    store_raw(db, key, raw)
    readers: tuple[Callable[[sqlite3.Connection], object], ...] = (load_settings, list_settings, lambda conn: get_setting(conn, key))
    for read in readers:
        with pytest.raises(SettingsError, match=re.escape(key)):
            read(db)
    assert reset_setting(db, key) is None
    assert load_settings(db) == Settings()
