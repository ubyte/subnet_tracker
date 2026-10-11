import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from subnet_tracker.core.db import transaction
from subnet_tracker.core.domains import Domain, get_domains
from subnet_tracker.core.edits import Keep
from subnet_tracker.core.events import EventKind, NewEvent
from subnet_tracker.core.global_routes import GlobalRoute, get_global_route
from subnet_tracker.core.groups import Group, GroupEdit, NewGroup, checked_group_edit, create_group, delete_group, edit_group, exclusive_member_ids, find_group, get_group, group_usage
from subnet_tracker.core.services.recording import Change, Recorded, details_of, record_change
from subnet_tracker.types import MAX_GROUP_NAME, FieldError, GroupId, JsonValue, Timestamp, network_order, one_line_name

# prefix and enabled feed the plan; the name and the description do not
_ROUTING = frozenset({"prefix_v4", "prefix_v6", "enabled"})


@dataclass(frozen=True, slots=True)
class GroupUpdate:
    group: Group
    changes: Mapping[str, Change]
    applied: bool


@dataclass(frozen=True, slots=True)
class GroupRemoval:
    group: Group
    domains: tuple[Domain, ...]
    routes: tuple[GlobalRoute, ...]
    applied: bool


def lookup_group(conn: sqlite3.Connection, name: str) -> Group | FieldError:
    checked = one_line_name("name", name, MAX_GROUP_NAME)
    if isinstance(checked, FieldError):
        return checked
    found = find_group(conn, checked)
    return found if found is not None else FieldError("name", f"{checked!r} does not exist")


def _diff(pairs: tuple[tuple[str, JsonValue, object], ...]) -> dict[str, Change]:
    changes: dict[str, Change] = {}
    for name, old, new in pairs:
        if isinstance(new, Keep) or new == old:
            continue
        if isinstance(new, (str, int, float)) or new is None:
            changes[name] = (old, new)
    return changes


def _changes(group: Group, edit: GroupEdit) -> dict[str, Change] | FieldError:
    checked = checked_group_edit(edit)
    if isinstance(checked, FieldError):
        return checked
    return _diff(
        (
            ("name", group.name, checked.name),
            ("description", group.description, checked.description),
            ("prefix_v4", group.prefix_v4, checked.prefix_v4),
            ("prefix_v6", group.prefix_v6, checked.prefix_v6),
            ("enabled", group.enabled, checked.enabled),
        )
    )


def _added(group: Group) -> dict[str, JsonValue]:
    return {"group_id": group.id, "name": group.name, "description": group.description, "prefix_v4": group.prefix_v4, "prefix_v6": group.prefix_v6, "enabled": group.enabled}


def add_group(conn: sqlite3.Connection, group: NewGroup, actor: str, now: Timestamp) -> Recorded[Group] | FieldError:
    with transaction(conn):
        created = create_group(conn, group, now)
        if isinstance(created, FieldError):
            return created
        applied = record_change(conn, [NewEvent(EventKind.GROUP_ADDED, actor, details=_added(created))], now, apply=False)
    return Recorded(created, applied)


def change_group(conn: sqlite3.Connection, group_id: GroupId, edit: GroupEdit, actor: str, now: Timestamp) -> GroupUpdate | FieldError:
    with transaction(conn):
        group = get_group(conn, group_id)
        if group is None:
            return FieldError("name", "does not exist")
        changes = _changes(group, edit)
        if isinstance(changes, FieldError) or not changes:
            return changes if isinstance(changes, FieldError) else GroupUpdate(group, {}, False)
        edited = edit_group(conn, group_id, edit, now)
        if not isinstance(edited, Group):
            return edited if isinstance(edited, FieldError) else FieldError("name", "does not exist")
        applied = record_change(conn, [NewEvent(EventKind.GROUP_CHANGED, actor, details={"group_id": edited.id, **details_of(changes)})], now, apply=bool(_ROUTING.intersection(changes)))
    return GroupUpdate(edited, changes, applied)


def _members(conn: sqlite3.Connection, group_id: GroupId, with_members: bool) -> tuple[tuple[Domain, ...], tuple[GlobalRoute, ...]]:
    if not with_members:
        return (), ()
    domain_ids, route_ids = exclusive_member_ids(conn, group_id)
    domains = tuple(sorted(get_domains(conn, domain_ids), key=lambda d: d.canonical.name))
    routes = tuple(sorted((r for i in route_ids if (r := get_global_route(conn, i)) is not None), key=lambda r: network_order(r.network)))
    return domains, routes


def remove_group(conn: sqlite3.Connection, group_id: GroupId, with_members: bool, actor: str, now: Timestamp) -> GroupRemoval | FieldError:
    with transaction(conn):
        group = get_group(conn, group_id)
        if group is None:
            return FieldError("name", "does not exist")
        domains, routes = _members(conn, group_id, with_members)
        used = group_usage(conn, group_id)
        if delete_group(conn, group_id, with_members) is None:
            return FieldError("name", "does not exist")
        events = [
            *(NewEvent(EventKind.DOMAIN_DELETED, actor, d.id, d.canonical.name) for d in domains),
            *(NewEvent(EventKind.ROUTE_DELETED, actor, details={"route_id": r.id, "cidr": str(r.network)}) for r in routes),
            NewEvent(EventKind.GROUP_DELETED, actor, details={"group_id": group.id, "name": group.name, "domains": len(domains), "routes": len(routes)}),
        ]
        applied = record_change(conn, events, now, apply=used != (0, 0))
    return GroupRemoval(group, domains, routes, applied)
