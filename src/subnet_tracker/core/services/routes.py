import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from subnet_tracker.core.db import transaction
from subnet_tracker.core.edits import Keep
from subnet_tracker.core.events import EventKind, NewEvent
from subnet_tracker.core.global_routes import (
    GlobalRoute,
    GlobalRouteEdit,
    NewGlobalRoute,
    checked_route_edit,
    create_global_route,
    delete_global_route,
    edit_global_route,
    edit_route_groups,
    find_global_route,
    get_global_route,
)
from subnet_tracker.core.groups import find_group, get_group
from subnet_tracker.core.memberships import SetChange
from subnet_tracker.core.services.recording import Change, details_of, record_change
from subnet_tracker.types import FieldError, GlobalRouteId, GroupId, IPAddress, JsonValue, Timestamp, route_network

# a comment does not change the plan
_ROUTING = frozenset({"cidr", "enabled", "groups"})


@dataclass(frozen=True, slots=True)
class RouteChange:
    edit: GlobalRouteEdit = field(default_factory=GlobalRouteEdit)
    add_groups: tuple[str, ...] = ()
    remove_groups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteUpdate:
    route: GlobalRoute
    changes: Mapping[str, Change]


def lookup_route(conn: sqlite3.Connection, text: str) -> GlobalRoute | FieldError:
    network = route_network("cidr", text.strip())
    if isinstance(network, FieldError):
        return network
    found = find_global_route(conn, network)
    return found if found is not None else FieldError("cidr", f"{network} does not exist")


def _group_ids(conn: sqlite3.Connection, names: Sequence[str]) -> frozenset[GroupId] | FieldError:
    ids: set[GroupId] = set()
    for name in names:
        if (group := find_group(conn, name)) is None:
            return FieldError("group", f"{name!r} does not exist")
        ids.add(group.id)
    return frozenset(ids)


def _names(conn: sqlite3.Connection, ids: frozenset[GroupId]) -> list[str]:
    return sorted(group.name for i in ids if (group := get_group(conn, i)) is not None)


def _field_changes(route: GlobalRoute, edit: GlobalRouteEdit, servers: Collection[IPAddress]) -> dict[str, Change] | FieldError:
    checked = checked_route_edit(edit, servers, route.network)
    if isinstance(checked, FieldError):
        return checked
    changes: dict[str, Change] = {}
    if not isinstance(checked.cidr, Keep) and checked.cidr != str(route.network):
        changes["cidr"] = (str(route.network), checked.cidr)
    if not isinstance(checked.comment, Keep) and checked.comment != route.comment:
        changes["comment"] = (route.comment, checked.comment)
    if not isinstance(checked.enabled, Keep) and checked.enabled != route.enabled:
        changes["enabled"] = (route.enabled, checked.enabled)
    return changes


def add_route(conn: sqlite3.Connection, cidr: str, comment: str, group_names: Sequence[str], servers: Collection[IPAddress], actor: str, now: Timestamp) -> GlobalRoute | FieldError:
    with transaction(conn):
        groups = _group_ids(conn, group_names)
        if isinstance(groups, FieldError):
            return groups
        created = create_global_route(conn, NewGlobalRoute(cidr, comment, groups=groups), servers, now)
        if isinstance(created, FieldError):
            return created
        record_change(conn, [NewEvent(EventKind.ROUTE_ADDED, actor, details={**_identity(created), "groups": _names(conn, created.groups)})], now, apply=True)
    return created


def _identity(route: GlobalRoute) -> dict[str, JsonValue]:
    return {"route_id": route.id, "cidr": str(route.network), "comment": route.comment, "enabled": route.enabled}


def change_route(conn: sqlite3.Connection, route_id: GlobalRouteId, change: RouteChange, servers: Collection[IPAddress], actor: str, now: Timestamp) -> RouteUpdate | FieldError:
    with transaction(conn):
        route = get_global_route(conn, route_id)
        if route is None:
            return FieldError("cidr", "does not exist")
        add = _group_ids(conn, change.add_groups)
        if isinstance(add, FieldError):
            return add
        remove = _group_ids(conn, change.remove_groups)
        if isinstance(remove, FieldError):
            return remove
        fields = _field_changes(route, change.edit, servers)
        if isinstance(fields, FieldError):
            return fields
        groups = (route.groups - remove) | add
        changes = dict(fields)
        if groups != route.groups:
            changes["groups"] = (_names(conn, route.groups), _names(conn, groups))
        if not changes:
            return RouteUpdate(route, {})
        edited = route
        if fields:
            result = edit_global_route(conn, route_id, change.edit, servers, now)
            if not isinstance(result, GlobalRoute):
                return result if isinstance(result, FieldError) else FieldError("cidr", "does not exist")
            edited = result
        if groups != route.groups:
            edit_route_groups(conn, [route_id], SetChange(remove=remove, add=add), now)
            refreshed = get_global_route(conn, route_id)
            if refreshed is None:
                return FieldError("cidr", "does not exist")
            edited = refreshed
        record_change(conn, [NewEvent(EventKind.ROUTE_CHANGED, actor, details={"route_id": edited.id, **details_of(changes)})], now, apply=bool(_ROUTING.intersection(changes)))
    return RouteUpdate(edited, changes)


def remove_route(conn: sqlite3.Connection, route_id: GlobalRouteId, actor: str, now: Timestamp) -> GlobalRoute | FieldError:
    with transaction(conn):
        route = get_global_route(conn, route_id)
        if route is None or not delete_global_route(conn, route_id):
            return FieldError("cidr", "does not exist")
        record_change(conn, [NewEvent(EventKind.ROUTE_DELETED, actor, details={"route_id": route.id, "cidr": str(route.network)})], now, apply=True)
    return route
