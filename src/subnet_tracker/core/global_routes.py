import json
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, replace
from ipaddress import ip_network

from subnet_tracker.config import IPNetwork
from subnet_tracker.core.db import transaction
from subnet_tracker.core.edits import KEEP, Keep, edit_columns, insert_row, taken, update_row
from subnet_tracker.core.groups import GROUP
from subnet_tracker.core.memberships import Membership, SetChange, Side, edit_memberships
from subnet_tracker.types import FieldError, GlobalRouteId, GroupId, IPAddress, Timestamp, network_order, one_line_text, route_network

ROUTE_GROUPS = Membership("route_groups", Side("global_routes", "route_id", GlobalRouteId), GROUP)
_SELECT = "SELECT r.*, (SELECT json_group_array(group_id) FROM route_groups WHERE route_id = r.id) AS groups FROM global_routes r"


@dataclass(frozen=True, slots=True)
class GlobalRoute:
    id: GlobalRouteId
    network: IPNetwork
    comment: str
    enabled: bool
    groups: frozenset[GroupId]
    created_at: Timestamp
    updated_at: Timestamp


@dataclass(frozen=True, slots=True)
class NewGlobalRoute:
    cidr: str
    comment: str = ""
    enabled: bool = True
    groups: frozenset[GroupId] = frozenset()


@dataclass(frozen=True, slots=True)
class GlobalRouteEdit:
    cidr: str | Keep = KEEP
    comment: str | Keep = KEEP
    enabled: bool | Keep = KEEP


# a route to the server through the tunnel breaks the tunnel itself
def server_address_error(network: IPNetwork, servers: Collection[IPAddress]) -> str | None:
    covered = next((ip for ip in servers if ip in network), None)
    return None if covered is None else f"covers the server address {covered}"


def global_route_network(text: str, servers: Collection[IPAddress]) -> IPNetwork | FieldError:
    network = route_network("cidr", text.strip())
    if isinstance(network, FieldError):
        return network
    return network if (error := server_address_error(network, servers)) is None else FieldError("cidr", error)


def checked_route_edit(edit: GlobalRouteEdit, servers: Collection[IPAddress]) -> GlobalRouteEdit | FieldError:
    network = edit.cidr if isinstance(edit.cidr, Keep) else global_route_network(edit.cidr, servers)
    if isinstance(network, FieldError):
        return network
    comment = edit.comment if isinstance(edit.comment, Keep) else one_line_text("comment", edit.comment)
    if isinstance(comment, FieldError):
        return comment
    return replace(edit, cidr=network if isinstance(network, Keep) else str(network), comment=comment)


def _columns(edit: GlobalRouteEdit) -> dict[str, object]:
    columns = edit_columns(edit)
    return columns if isinstance(edit.cidr, Keep) else columns | {"family": ip_network(edit.cidr).version}


def _route(row: sqlite3.Row) -> GlobalRoute:
    groups = frozenset(GroupId(g) for g in json.loads(row["groups"]))
    return GlobalRoute(GlobalRouteId(row["id"]), ip_network(row["cidr"]), row["comment"], bool(row["enabled"]), groups, Timestamp(row["created_at"]), Timestamp(row["updated_at"]))


def _routes(conn: sqlite3.Connection, where: str = "", *values: object) -> list[GlobalRoute]:
    return [_route(row) for row in conn.execute(_SELECT + where, values)]


def get_global_route(conn: sqlite3.Connection, route_id: GlobalRouteId) -> GlobalRoute | None:
    return next(iter(_routes(conn, " WHERE r.id = ?", route_id)), None)


def find_global_route(conn: sqlite3.Connection, network: IPNetwork) -> GlobalRoute | None:
    return next(iter(_routes(conn, " WHERE r.cidr = ?", str(network))), None)


def list_global_routes(conn: sqlite3.Connection) -> tuple[GlobalRoute, ...]:
    return tuple(sorted(_routes(conn), key=lambda r: network_order(r.network)))


def create_global_route(conn: sqlite3.Connection, route: NewGlobalRoute, servers: Collection[IPAddress], now: Timestamp) -> GlobalRoute | FieldError:
    checked = checked_route_edit(GlobalRouteEdit(route.cidr, route.comment, route.enabled), servers)
    if isinstance(checked, FieldError):
        return checked
    with transaction(conn):
        if error := taken(conn, "global_routes", "cidr", "global route", checked.cidr, None):
            return error
        route_id = GlobalRouteId(insert_row(conn, "global_routes", _columns(checked), now)["id"])
        edit_memberships(conn, ROUTE_GROUPS, [route_id], SetChange(replace=route.groups), now)
        (created,) = _routes(conn, " WHERE r.id = ?", route_id)
    return created


def edit_global_route(conn: sqlite3.Connection, route_id: GlobalRouteId, edit: GlobalRouteEdit, servers: Collection[IPAddress], now: Timestamp) -> GlobalRoute | FieldError | None:
    checked = checked_route_edit(edit, servers)
    if isinstance(checked, FieldError):
        return checked
    with transaction(conn):
        if error := taken(conn, "global_routes", "cidr", "global route", checked.cidr, route_id):
            return error
        update_row(conn, "global_routes", route_id, _columns(checked), now)
        return get_global_route(conn, route_id)


def edit_route_groups(conn: sqlite3.Connection, route_ids: Collection[GlobalRouteId], change: SetChange[GroupId], now: Timestamp) -> int:
    return edit_memberships(conn, ROUTE_GROUPS, route_ids, change, now)


def delete_global_route(conn: sqlite3.Connection, route_id: GlobalRouteId) -> bool:
    return conn.execute("DELETE FROM global_routes WHERE id = ?", (route_id,)).rowcount > 0
