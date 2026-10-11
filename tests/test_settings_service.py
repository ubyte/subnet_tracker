import sqlite3

import pytest

from db_rows import insert
from domains_world import NOW, count
from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import Event, EventFilter, EventKind, list_events
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.core.services.settings import change_setting
from subnet_tracker.core.settings import SettingError, SettingUpdate, SettingValue, StoredSetting, parse_setting, setting_update
from subnet_tracker.core.settings_store import get_setting, load_settings
from subnet_tracker.types import EventId, ReconnectMode, Seconds, Timestamp

ACTOR = "cli:alice"
KEY = "resolve.interval"


def interval(text: str) -> SettingValue:
    value = parse_setting(KEY, text)
    assert isinstance(value, SettingValue)
    return value


def written(db: sqlite3.Connection) -> tuple[int, int, int]:
    return count(db, "settings"), count(db, "events"), count(db, "requests")


@pytest.mark.parametrize(
    ("stored", "text", "expected"),
    [
        (None, "90m", SettingUpdate(interval("90m"), "1h", True)),
        (None, "60m", SettingUpdate(interval("1h"), "1h", False)),
        (None, None, SettingUpdate(interval("1h"), "1h", False)),
        ('"90m"', "5400s", SettingUpdate(interval("90m"), "90m", False)),
        ('"90m"', "2h", SettingUpdate(interval("2h"), "90m", True)),
        ('"90m"', "1h", SettingUpdate(interval("1h"), "90m", True)),
        ('"90m"', None, SettingUpdate(interval("1h"), "90m", True)),
        ('"60m"', "1h", SettingUpdate(interval("1h"), "1h", True)),
        ('"0s"', "2h", SettingUpdate(interval("2h"), None, True)),
        ('"0s"', None, SettingUpdate(interval("1h"), None, True)),
    ],
)
def test_update_changes_only_when_the_row_changes(stored: str | None, text: str | None, expected: SettingUpdate) -> None:
    assert setting_update(KEY, None if stored is None else StoredSetting(stored, Timestamp(0)), text) == expected


@pytest.mark.parametrize("text", ["2h", None])
def test_update_of_unknown_key_is_rejected(text: str | None) -> None:
    assert setting_update("domains.www_pair", None, text) == SettingError("domains.www_pair", "unknown setting 'domains.www_pair'")


def test_update_with_invalid_text_is_rejected() -> None:
    assert setting_update(KEY, None, "0s") == SettingError(KEY, "must be at least 1s")


def test_error_text_names_a_known_key_once() -> None:
    assert (str(SettingError(KEY, "must be at least 1s")), str(SettingError("x.y", "unknown setting 'x.y'"))) == ("resolve.interval: must be at least 1s", "unknown setting 'x.y'")


def test_change_writes_value_event_and_apply_request(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    update = change_setting(db, "routes.retention", "7d", ACTOR, NOW)
    assert isinstance(update, SettingUpdate)
    assert update.changed
    assert not db.in_transaction
    assert load_settings(other).routes.retention == Seconds(7 * 86400)
    assert list_events(other, EventFilter(10)) == (Event(EventId(1), NOW, EventKind.SETTING_CHANGED, ACTOR, None, None, {"key": "routes.retention", "old": "4d", "new": "7d"}),)
    claim = claim_requests(other)
    assert claim is not None
    assert claim.work == TickWork(apply=ReconnectMode.AUTO)


def test_reset_removes_the_row_and_records_the_default(db: sqlite3.Connection) -> None:
    change_setting(db, KEY, "90m", ACTOR, NOW)
    update = change_setting(db, KEY, None, ACTOR, Timestamp(NOW + 1))
    assert update == SettingUpdate(interval("1h"), "90m", True)
    assert written(db) == (0, 2, 2)
    assert list_events(db, EventFilter(1))[0].details == {"key": KEY, "old": "90m", "new": "1h"}


def test_reset_repairs_an_invalid_stored_value(db: sqlite3.Connection) -> None:
    insert(db, "settings", {"key": KEY, "value": '"0s"', "updated_at": 0})
    assert change_setting(db, KEY, None, ACTOR, NOW) == SettingUpdate(interval("1h"), None, True)
    assert list_events(db, EventFilter(1))[0].details == {"key": KEY, "old": None, "new": "1h"}
    assert load_settings(db).resolve.interval == Seconds(3600)


@pytest.mark.parametrize("text", ["60m", None])
def test_same_value_writes_nothing(db: sqlite3.Connection, text: str | None) -> None:
    update = change_setting(db, KEY, text, ACTOR, NOW)
    assert isinstance(update, SettingUpdate)
    assert not update.changed
    assert written(db) == (0, 0, 0)


def test_same_stored_value_keeps_its_time(db: sqlite3.Connection) -> None:
    change_setting(db, KEY, "90m", ACTOR, NOW)
    change_setting(db, KEY, "5400s", ACTOR, Timestamp(NOW + 1))
    state = get_setting(db, KEY)
    assert not isinstance(state, SettingError)
    assert state.updated_at == NOW
    assert written(db) == (1, 1, 1)


@pytest.mark.parametrize(("key", "text"), [("domains.www_pair", "all"), (KEY, "0s"), ("domains.www_pair", None)])
def test_rejected_change_writes_nothing(db: sqlite3.Connection, key: str, text: str | None) -> None:
    assert isinstance(change_setting(db, key, text, ACTOR, NOW), SettingError)
    assert written(db) == (0, 0, 0)


def change_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        change_setting(db, KEY, "90m", ACTOR, NOW)
        raise RuntimeError


def test_change_rolls_back_with_the_outer_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        change_and_fail(db)
    assert written(db) == (0, 0, 0)
