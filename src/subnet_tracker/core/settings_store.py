import sqlite3

from subnet_tracker.core.db import transaction
from subnet_tracker.core.settings import SettingError, Settings, SettingState, SettingValue, StoredSetting, check_key, parse_setting, setting_state, setting_states, settings_from
from subnet_tracker.types import Timestamp

_UPSERT = "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"


def _stored(row: sqlite3.Row) -> StoredSetting:
    return StoredSetting(row["value"], Timestamp(row["updated_at"]))


def _all_stored(conn: sqlite3.Connection) -> dict[str, StoredSetting]:
    return {row["key"]: _stored(row) for row in conn.execute("SELECT key, value, updated_at FROM settings")}


def load_settings(conn: sqlite3.Connection) -> Settings:
    return settings_from(_all_stored(conn))


def list_settings(conn: sqlite3.Connection) -> tuple[SettingState, ...]:
    return setting_states(_all_stored(conn))


def stored_setting(conn: sqlite3.Connection, key: str) -> StoredSetting | None:
    row = conn.execute("SELECT value, updated_at FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else _stored(row)


def get_setting(conn: sqlite3.Connection, key: str) -> SettingState | SettingError:
    return setting_state(key, stored_setting(conn, key))


def save_setting(conn: sqlite3.Connection, value: SettingValue, now: Timestamp) -> None:
    if value.is_default:
        conn.execute("DELETE FROM settings WHERE key = ?", (value.key,))
    else:
        conn.execute(_UPSERT, (value.key, value.json, now))


def set_setting(conn: sqlite3.Connection, key: str, text: str, now: Timestamp) -> SettingValue | SettingError:
    value = parse_setting(key, text)
    if isinstance(value, SettingValue):
        with transaction(conn):
            save_setting(conn, value, now)
    return value


def reset_setting(conn: sqlite3.Connection, key: str) -> SettingError | None:
    if (error := check_key(key)) is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    return error
