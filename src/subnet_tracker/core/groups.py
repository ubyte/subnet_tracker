import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, replace

from subnet_tracker.core.db import transaction
from subnet_tracker.core.edits import KEEP, Keep, edit_columns, prefixes_error
from subnet_tracker.core.memberships import Membership, SetChange, edit_memberships
from subnet_tracker.types import MAX_GROUP_NAME, DomainId, FieldError, GroupId, Timestamp, one_line_name, one_line_text

GROUPS = Membership("domain_groups", "group_id", "groups", GroupId)
_COUNTS = "(SELECT count(*) FROM domain_groups WHERE group_id = g.id) AS domains, (SELECT count(*) FROM route_groups WHERE group_id = g.id) AS routes"
_MEMBERS = (("domains", "domain_groups", "domain_id"), ("global_routes", "route_groups", "route_id"))


@dataclass(frozen=True, slots=True)
class Group:
    id: GroupId
    name: str
    description: str
    prefix_v4: int | None
    prefix_v6: int | None
    enabled: bool
    created_at: Timestamp
    updated_at: Timestamp


@dataclass(frozen=True, slots=True)
class NewGroup:
    name: str
    description: str = ""
    prefix_v4: int | None = None
    prefix_v6: int | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class GroupEdit:
    name: str | Keep = KEEP
    description: str | Keep = KEEP
    # None sets the prefix back to inherited
    prefix_v4: int | Keep | None = KEEP
    prefix_v6: int | Keep | None = KEEP
    enabled: bool | Keep = KEEP


@dataclass(frozen=True, slots=True)
class GroupRow:
    group: Group | None  # None is "No group": domains and global routes without any group
    domains: int
    routes: int


@dataclass(frozen=True, slots=True)
class DeletedGroup:
    # members deleted with the group because it was their only one
    domains: int
    routes: int


def checked_group_edit(edit: GroupEdit) -> GroupEdit | FieldError:
    if error := prefixes_error(edit.prefix_v4, edit.prefix_v6):
        return error
    name = edit.name if isinstance(edit.name, Keep) else one_line_name("name", edit.name, MAX_GROUP_NAME)
    if isinstance(name, FieldError):
        return name
    description = edit.description if isinstance(edit.description, Keep) else one_line_text("description", edit.description)
    if isinstance(description, FieldError):
        return description
    return replace(edit, name=name, description=description)


def _group(row: sqlite3.Row) -> Group:
    return Group(GroupId(row["id"]), row["name"], row["description"], row["prefix_v4"], row["prefix_v6"], bool(row["enabled"]), Timestamp(row["created_at"]), Timestamp(row["updated_at"]))


def _name_taken(conn: sqlite3.Connection, name: str | Keep, group_id: GroupId | None) -> FieldError | None:
    if isinstance(name, str) and conn.execute("SELECT 1 FROM groups WHERE name = ? AND id IS NOT ?", (name, group_id)).fetchone():
        return FieldError("name", f"group {name!r} already exists")
    return None


def get_group(conn: sqlite3.Connection, group_id: GroupId) -> Group | None:
    row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    return None if row is None else _group(row)


def find_group(conn: sqlite3.Connection, name: str) -> Group | None:
    row = conn.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()
    return None if row is None else _group(row)


def create_group(conn: sqlite3.Connection, group: NewGroup, now: Timestamp) -> Group | FieldError:
    checked = checked_group_edit(GroupEdit(group.name, group.description, group.prefix_v4, group.prefix_v6, group.enabled))
    if isinstance(checked, FieldError):
        return checked
    columns = edit_columns(checked)
    with transaction(conn):
        if error := _name_taken(conn, checked.name, None):
            return error
        row = conn.execute(f"INSERT INTO groups ({', '.join(columns)}, created_at, updated_at) VALUES ({', '.join('?' * len(columns))}, ?, ?) RETURNING *", (*columns.values(), now, now)).fetchone()
    return _group(row)


def edit_group(conn: sqlite3.Connection, group_id: GroupId, edit: GroupEdit, now: Timestamp) -> Group | FieldError | None:
    checked = checked_group_edit(edit)
    if isinstance(checked, FieldError):
        return checked
    columns = edit_columns(checked)
    with transaction(conn):
        if error := _name_taken(conn, checked.name, group_id):
            return error
        if columns:
            assignments = ", ".join(f"{column} = ?" for column in columns)
            conn.execute(f"UPDATE groups SET {assignments}, updated_at = ? WHERE id = ?", (*columns.values(), now, group_id))
        return get_group(conn, group_id)


def _delete_only_members(conn: sqlite3.Connection, group_id: GroupId) -> DeletedGroup:
    only_in = "SELECT {0} FROM {1} GROUP BY {0} HAVING count(*) = 1 AND max(group_id) = ?"
    return DeletedGroup(*(conn.execute(f"DELETE FROM {table} WHERE id IN ({only_in.format(member, links)})", (group_id,)).rowcount for table, links, member in _MEMBERS))


def delete_group(conn: sqlite3.Connection, group_id: GroupId, with_members: bool = False) -> DeletedGroup | None:
    with transaction(conn):
        deleted = _delete_only_members(conn, group_id) if with_members else DeletedGroup(0, 0)
        found = conn.execute("DELETE FROM groups WHERE id = ?", (group_id,)).rowcount
    return deleted if found else None


def list_groups(conn: sqlite3.Connection) -> tuple[GroupRow, ...]:
    no_group = (conn.execute(f"SELECT count(*) FROM {table} WHERE id NOT IN (SELECT {member} FROM {links})").fetchone()[0] for table, links, member in _MEMBERS)
    rows = conn.execute(f"SELECT g.*, {_COUNTS} FROM groups g ORDER BY g.name")
    return (GroupRow(None, *no_group), *(GroupRow(_group(row), row["domains"], row["routes"]) for row in rows))


def edit_domain_groups(conn: sqlite3.Connection, domain_ids: Collection[DomainId], change: SetChange[GroupId], now: Timestamp) -> int:
    return edit_memberships(conn, GROUPS, domain_ids, change, now)
