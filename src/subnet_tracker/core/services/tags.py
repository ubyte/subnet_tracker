import sqlite3
from dataclasses import dataclass

from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import EventKind, NewEvent
from subnet_tracker.core.services.recording import Recorded, record_change
from subnet_tracker.core.tags import Tag, delete_tag, find_tag, get_tag, rename_tag
from subnet_tracker.types import MAX_TAG_NAME, FieldError, TagId, Timestamp, one_line_name


@dataclass(frozen=True, slots=True)
class TagRename:
    tag: Tag
    old: str
    merged: bool
    changed: bool
    applied: bool


def lookup_tag(conn: sqlite3.Connection, name: str) -> Tag | FieldError:
    checked = one_line_name("name", name, MAX_TAG_NAME)
    if isinstance(checked, FieldError):
        return checked
    found = find_tag(conn, checked)
    return found if found is not None else FieldError("name", f"{checked!r} does not exist")


def rename_named_tag(conn: sqlite3.Connection, old: str, new: str, actor: str, now: Timestamp) -> TagRename | FieldError:
    with transaction(conn):
        current = lookup_tag(conn, old)
        if isinstance(current, FieldError):
            return current
        checked = one_line_name("name", new, MAX_TAG_NAME)
        if isinstance(checked, FieldError):
            return checked
        if checked == current.name:
            return TagRename(current, current.name, False, False, False)
        renamed = rename_tag(conn, current.id, checked)
        if isinstance(renamed, FieldError):
            return renamed
        if renamed is None:
            return FieldError("name", f"{current.name!r} does not exist")
        merged = renamed.id != current.id
        kind = EventKind.TAG_MERGED if merged else EventKind.TAG_RENAMED
        applied = record_change(conn, [NewEvent(kind, actor, details={"tag_id": renamed.id, "old": current.name, "new": renamed.name})], now, apply=False)
    return TagRename(renamed, current.name, merged, True, applied)


def remove_tag(conn: sqlite3.Connection, tag_id: TagId, actor: str, now: Timestamp) -> Recorded[Tag] | FieldError:
    with transaction(conn):
        tag = get_tag(conn, tag_id)
        if tag is None or not delete_tag(conn, tag_id):
            return FieldError("name", "does not exist")
        applied = record_change(conn, [NewEvent(EventKind.TAG_DELETED, actor, details={"tag_id": tag.id, "name": tag.name})], now, apply=False)
    return Recorded(tag, applied)
