import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from db_rows import insert
from settings_samples import SAMPLE_SETTINGS, SAMPLES
from subnet_tracker.core.db import connect
from subnet_tracker.core.settings import SETTING_KEYS, SettingError, Settings, SettingsError, SettingState, SettingValue, setting_states
from subnet_tracker.core.settings_store import get_setting, list_settings, load_settings, reset_setting, set_setting
from subnet_tracker.types import Timestamp

NOW = Timestamp(1_760_000_000)


def stored(db: sqlite3.Connection, key: str) -> object:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else json.loads(row["value"])


def store_raw(db: sqlite3.Connection, key: str, value: str) -> None:
    insert(db, "settings", {"key": key, "value": value, "updated_at": 0})


def test_empty_db_has_defaults(db: sqlite3.Connection) -> None:
    assert load_settings(db) == Settings()
    assert list_settings(db) == setting_states({})


def test_unknown_key_rejected(db: sqlite3.Connection) -> None:
    for error in (get_setting(db, "domains.www_pair"), set_setting(db, "domains.www_pair", "all", NOW), reset_setting(db, "domains.www_pair")):
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
