import sqlite3
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace

from subnet_tracker.core.db import snapshot, transaction
from subnet_tracker.core.edits import KEEP, Keep, edit_columns, insert_row, only_linked, prefixes_error, taken, update_row
from subnet_tracker.core.memberships import DOMAIN, Membership, SetChange, Side, edit_memberships
from subnet_tracker.types import MAX_GROUP_NAME, DomainId, FieldError, GlobalRouteId, GroupId, Timestamp, one_line_name, one_line_text

# the filter keyword for domains without a group, so no group may take it as a name
NO_GROUP = "none"
GROUP = Side("groups", "group_id", GroupId)
GROUPS = Membership("domain_groups", DOMAIN, GROUP)
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
    if isinstance(name, str) and name.casefold() == NO_GROUP:
        return FieldError("name", f"{name!r} is reserved for domains without a group")
    description = edit.description if isinstance(edit.description, Keep) else one_line_text("description", edit.description)
    if isinstance(description, FieldError):
        return description
    return replace(edit, name=name, description=description)


def _group(row: sqlite3.Row) -> Group:
    return Group(GroupId(row["id"]), row["name"], row["description"], row["prefix_v4"], row["prefix_v6"], bool(row["enabled"]), Timestamp(row["created_at"]), Timestamp(row["updated_at"]))


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
    with transaction(conn):
        if error := taken(conn, "groups", "name", "group", checked.name, None):
            return error
        return _group(insert_row(conn, "groups", edit_columns(checked), now))


def edit_group(conn: sqlite3.Connection, group_id: GroupId, edit: GroupEdit, now: Timestamp) -> Group | FieldError | None:
    checked = checked_group_edit(edit)
    if isinstance(checked, FieldError):
        return checked
    with transaction(conn):
        if get_group(conn, group_id) is None:
            return None
        if error := taken(conn, "groups", "name", "group", checked.name, group_id):
            return error
        update_row(conn, "groups", group_id, edit_columns(checked), now)
        return get_group(conn, group_id)


def _delete_only_members(conn: sqlite3.Connection, group_id: GroupId) -> DeletedGroup:
    return DeletedGroup(*(conn.execute(f"DELETE FROM {table} WHERE id IN ({only_linked(links, member, 'group_id')})", (group_id,)).rowcount for table, links, member in _MEMBERS))


def delete_group(conn: sqlite3.Connection, group_id: GroupId, with_members: bool = False) -> DeletedGroup | None:
    with transaction(conn):
        deleted = _delete_only_members(conn, group_id) if with_members else DeletedGroup(0, 0)
        found = conn.execute("DELETE FROM groups WHERE id = ?", (group_id,)).rowcount
    return deleted if found else None


def list_groups(conn: sqlite3.Connection) -> tuple[GroupRow, ...]:
    with snapshot(conn):
        no_group = [conn.execute(f"SELECT count(*) FROM {table} WHERE id NOT IN (SELECT {member} FROM {links})").fetchone()[0] for table, links, member in _MEMBERS]
        rows = conn.execute(f"SELECT g.*, {_COUNTS} FROM groups g ORDER BY g.name").fetchall()
    return (GroupRow(None, *no_group), *(GroupRow(_group(row), row["domains"], row["routes"]) for row in rows))


def edit_domain_groups(conn: sqlite3.Connection, domain_ids: Collection[DomainId], change: SetChange[GroupId], now: Timestamp) -> int:
    return edit_memberships(conn, GROUPS, domain_ids, change, now)


def group_usage(conn: sqlite3.Connection, group_id: GroupId) -> tuple[int, int]:
    def count(table: str) -> int:
        return int(conn.execute(f"SELECT count(*) FROM {table} WHERE group_id = ?", (group_id,)).fetchone()[0])

    return count("domain_groups"), count("route_groups")


def exclusive_member_ids(conn: sqlite3.Connection, group_id: GroupId) -> tuple[tuple[DomainId, ...], tuple[GlobalRouteId, ...]]:
    def column[T](table: str, links: str, member: str, make: Callable[[int], T]) -> tuple[T, ...]:
        rows = conn.execute(f"SELECT id FROM {table} WHERE id IN ({only_linked(links, member, 'group_id')}) ORDER BY id", (group_id,))
        return tuple(make(row[0]) for row in rows)

    return column("domains", "domain_groups", "domain_id", DomainId), column("global_routes", "route_groups", "route_id", GlobalRouteId)
