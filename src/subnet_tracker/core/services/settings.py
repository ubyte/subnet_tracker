import sqlite3

from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import EventKind, NewEvent, record_events
from subnet_tracker.core.requests import Apply, enqueue
from subnet_tracker.core.settings import SettingError, SettingUpdate, setting_update
from subnet_tracker.core.settings_store import save_setting, stored_setting
from subnet_tracker.types import ReconnectMode, Timestamp


def setting_changed(update: SettingUpdate, actor: str) -> NewEvent:
    return NewEvent(EventKind.SETTING_CHANGED, actor, details={"key": update.new.key, "old": update.old, "new": update.new.text})


# text None resets the setting; routes the change adds reconnect in auto mode, like every change but adding domains (8.5)
def change_setting(conn: sqlite3.Connection, key: str, text: str | None, actor: str, now: Timestamp) -> SettingUpdate | SettingError:
    with transaction(conn):
        update = setting_update(key, stored_setting(conn, key), text)
        if isinstance(update, SettingUpdate) and update.changed:
            save_setting(conn, update.new, now)
            record_events(conn, [setting_changed(update, actor)], now)
            enqueue(conn, [Apply(ReconnectMode.AUTO)], now)
    return update
