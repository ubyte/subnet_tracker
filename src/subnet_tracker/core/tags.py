import json
import sqlite3
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from subnet_tracker.core.db import transaction
from subnet_tracker.core.memberships import DOMAIN, Membership, SetChange, Side, edit_memberships
from subnet_tracker.types import MAX_TAG_NAME, DomainId, FieldError, TagId, Timestamp, one_line_name

TAGS = Membership("domain_tags", DOMAIN, Side("tags", "tag_id", TagId))


@dataclass(frozen=True, slots=True)
class Tag:
    id: TagId
    name: str


@dataclass(frozen=True, slots=True)
class TagRow:
    tag: Tag
    domains: int


def checked_tag_names(names: Iterable[str]) -> tuple[str, ...] | FieldError:
    checked: dict[str, None] = {}
    for text in names:
        name = one_line_name("name", text, MAX_TAG_NAME)
        if isinstance(name, FieldError):
            return name
        checked[name] = None
    return tuple(checked)


def _tag(row: sqlite3.Row) -> Tag:
    return Tag(TagId(row["id"]), row["name"])


def find_tag(conn: sqlite3.Connection, name: str) -> Tag | None:
    row = conn.execute("SELECT id, name FROM tags WHERE name = ?", (name,)).fetchone()
    return None if row is None else _tag(row)


def list_tags(conn: sqlite3.Connection) -> tuple[TagRow, ...]:
    rows = conn.execute("SELECT t.id, t.name, count(dt.domain_id) AS domains FROM tags t LEFT JOIN domain_tags dt ON dt.tag_id = t.id GROUP BY t.id ORDER BY t.name")
    return tuple(TagRow(_tag(row), row["domains"]) for row in rows)


def find_tags(conn: sqlite3.Connection, names: Iterable[str]) -> frozenset[TagId]:
    return frozenset(TagId(i) for (i,) in conn.execute("SELECT id FROM tags WHERE name IN (SELECT value FROM json_each(?))", (json.dumps(list(names)),)))


# names checked by checked_tag_names
def insert_tags(conn: sqlite3.Connection, names: Sequence[str]) -> frozenset[TagId]:
    with transaction(conn):
        conn.executemany("INSERT INTO tags (name) VALUES (?) ON CONFLICT (name) DO NOTHING", [(name,) for name in names])
        return find_tags(conn, names)


def ensure_tags(conn: sqlite3.Connection, names: Iterable[str]) -> frozenset[TagId] | FieldError:
    checked = checked_tag_names(names)
    return checked if isinstance(checked, FieldError) else insert_tags(conn, checked)


def rename_tag(conn: sqlite3.Connection, tag_id: TagId, name: str) -> Tag | FieldError | None:
    checked = one_line_name("name", name, MAX_TAG_NAME)
    if isinstance(checked, FieldError):
        return checked
    with transaction(conn):
        if conn.execute("SELECT 1 FROM tags WHERE id = ?", (tag_id,)).fetchone() is None:
            return None
        if (taken := find_tag(conn, checked)) is None:
            conn.execute("UPDATE tags SET name = ? WHERE id = ?", (checked, tag_id))
            return Tag(tag_id, checked)
        if taken.id != tag_id:
            # renaming to a taken name merges the tag into the one that has it
            conn.execute("INSERT OR IGNORE INTO domain_tags (domain_id, tag_id) SELECT domain_id, ? FROM domain_tags WHERE tag_id = ?", (taken.id, tag_id))
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        return taken


def delete_tag(conn: sqlite3.Connection, tag_id: TagId) -> bool:
    return conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,)).rowcount > 0


def edit_domain_tags(conn: sqlite3.Connection, domain_ids: Collection[DomainId], change: SetChange[TagId], now: Timestamp) -> int:
    return edit_memberships(conn, TAGS, domain_ids, change, now)
