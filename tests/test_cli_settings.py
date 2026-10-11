import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from cli_world import ACTOR, NOW, Cli, Result
from db_rows import insert
from domains_world import count
from subnet_tracker.cli.commands.settings import settings_table, update_text
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.core.settings import SETTING_KEYS, SettingState, SettingUpdate, SettingValue, parse_setting
from subnet_tracker.core.settings_store import load_settings, set_setting, stored_setting
from subnet_tracker.types import ReconnectMode, Seconds, Timestamp


def written(db: sqlite3.Connection) -> tuple[int, int, int]:
    return count(db, "settings"), count(db, "events"), count(db, "requests")


def trigger_lines(tmp_path: Path) -> list[str]:
    trigger = tmp_path / "trigger"
    return trigger.read_text().splitlines() if trigger.exists() else []


def test_table_has_key_value_and_default() -> None:
    states = (SettingState("a.b", "1", "1", None), SettingState("long.key", "x", "", NOW))
    assert settings_table(states) == "key       value  default\na.b       1      1\nlong.key  x"


@pytest.mark.parametrize(("changed", "text"), [(True, "resolve.interval = 2h"), (False, "resolve.interval = 2h (unchanged)")])
def test_update_text_says_when_nothing_changed(changed: bool, text: str) -> None:
    value = parse_setting("resolve.interval", "2h")
    assert isinstance(value, SettingValue)
    assert update_text(SettingUpdate(value, "2h", changed)) == text


def test_list_shows_every_setting_in_registry_order(cli: Cli, db: sqlite3.Connection) -> None:
    set_setting(db, "resolve.interval", "90m", NOW)
    result = cli("settings", "list")
    assert (result.code, result.err) == (0, "")
    header, *rows = result.out.splitlines()
    assert header.split() == ["key", "value", "default"]
    assert [row.split()[0] for row in rows] == list(SETTING_KEYS)
    assert rows[0].split() == ["resolve.interval", "90m", "1h"]


def test_list_json(cli: Cli, db: sqlite3.Connection) -> None:
    set_setting(db, "resolve.interval", "90m", NOW)
    data = cli.json("settings", "list")
    assert isinstance(data, list)
    assert data[:2] == [{"key": "resolve.interval", "value": "90m", "default": "1h", "updated_at": NOW}, {"key": "resolve.timeout", "value": "3s", "default": "3s", "updated_at": None}]
    assert [item["key"] for item in data if isinstance(item, dict)] == list(SETTING_KEYS)


def test_get_prints_the_value_alone(cli: Cli, db: sqlite3.Connection) -> None:
    assert cli("settings", "get", "discovery.ignore") == Result(0, "w3.org, schema.org, ogp.me\n", "")


def test_get_json(cli: Cli, db: sqlite3.Connection) -> None:
    assert cli.json("settings", "get", "routes.exclude") == {"key": "routes.exclude", "value": "", "default": "", "updated_at": None}


@pytest.mark.parametrize("argv", [("get", "x.y"), ("set", "x.y", "1"), ("reset", "x.y")])
def test_unknown_key_is_a_usage_error(cli: Cli, db: sqlite3.Connection, argv: tuple[str, ...]) -> None:
    assert cli("settings", *argv) == Result(2, "", "subnet-tracker: error: unknown setting 'x.y'\n")
    assert written(db) == (0, 0, 0)


def test_invalid_stored_value_is_an_error_until_reset(cli: Cli, db: sqlite3.Connection) -> None:
    insert(db, "settings", {"key": "resolve.interval", "value": '"0s"', "updated_at": 0})
    for argv in (("list",), ("get", "resolve.interval")):
        result = cli("settings", *argv)
        assert (result.code, result.out) == (1, "")
        assert "subnet-tracker settings reset resolve.interval" in result.err
    assert cli("settings", "reset", "resolve.interval") == Result(0, "resolve.interval = 1h\n", "")
    assert list_events(db, EventFilter(1))[0].details == {"key": "resolve.interval", "old": None, "new": "1h"}
    assert cli("settings", "list").code == 0


def test_set_writes_value_event_request_and_trigger(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert cli("settings", "set", "routes.exclude", "10.0.0.0/8 10.0.0.0/8") == Result(0, "routes.exclude = 10.0.0.0/8\n", "")
    assert [str(net) for net in load_settings(db).routes.exclude] == ["10.0.0.0/8"]
    (event,) = list_events(db, EventFilter(10))
    assert (event.ts, event.kind, event.actor, event.details) == (NOW, EventKind.SETTING_CHANGED, ACTOR, {"key": "routes.exclude", "old": "", "new": "10.0.0.0/8"})
    claim = claim_requests(db)
    assert claim is not None
    assert claim.work == TickWork(apply=ReconnectMode.AUTO)
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]


def test_set_json(cli: Cli, db: sqlite3.Connection) -> None:
    assert cli.json("settings", "set", "resolve.interval", "120m") == {"key": "resolve.interval", "value": "2h", "old": "1h", "changed": True}


def test_invalid_value_is_a_usage_error(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert cli("settings", "set", "resolve.interval", "0s") == Result(2, "", "subnet-tracker: error: resolve.interval: must be at least 1s\n")
    assert written(db) == (0, 0, 0)
    assert trigger_lines(tmp_path) == []


def test_same_value_notifies_nobody(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert cli("settings", "set", "resolve.interval", "60m") == Result(0, "resolve.interval = 1h (unchanged)\n", "")
    assert written(db) == (0, 0, 0)
    assert trigger_lines(tmp_path) == []


def test_reset_removes_the_row(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    cli("settings", "set", "resolve.interval", "90m")
    assert cli("settings", "reset", "resolve.interval") == Result(0, "resolve.interval = 1h\n", "")
    assert stored_setting(db, "resolve.interval") is None
    assert written(db) == (0, 2, 2)
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"] * 2


def test_unwritable_trigger_is_a_warning(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    broken = replace(cli, runtime=replace(cli.runtime, trigger_file=tmp_path / "absent" / "trigger"))
    result = broken("settings", "set", "resolve.interval", "90m")
    assert (result.code, result.out) == (0, "resolve.interval = 90m\n")
    assert result.err.startswith("subnet-tracker: warning: tick not notified: ")
    assert result.err.endswith("the next scheduled tick picks the change up\n")
    assert load_settings(db).resolve.interval == Seconds(5400)


def test_time_comes_from_the_runtime(cli: Cli, db: sqlite3.Connection) -> None:
    later = replace(cli, runtime=replace(cli.runtime, clock=lambda: Timestamp(NOW + 5)))
    later("settings", "set", "resolve.interval", "90m")
    stored = stored_setting(db, "resolve.interval")
    assert stored is not None
    assert stored.updated_at == NOW + 5
