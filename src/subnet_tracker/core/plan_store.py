import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address, ip_network

from subnet_tracker.config import IPNetwork
from subnet_tracker.core.db import transaction
from subnet_tracker.routes.model import (
    BudgetWarning,
    Exclusion,
    ExclusionReason,
    GlobalRouteWarning,
    Part,
    PlanStats,
    PlanWarning,
    RoutePlan,
    RouteWarning,
    WarningKind,
    route_order,
)
from subnet_tracker.types import (
    ApplyRunId,
    ApplyStatus,
    ApplyTrigger,
    GlobalRouteId,
    IPAddress,
    JsonValue,
    PendingReconnect,
    PlanRouteId,
    ReconnectMode,
    RouteKind,
    Timestamp,
    network_order,
)

_STATE = """
INSERT INTO plan_state (id, computed_at, plan_sha256, routes_v4, routes_v6, raw_v4, raw_v6, extra_ratio_v4, extra_ratio_v6, warnings)
VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
  computed_at = excluded.computed_at, plan_sha256 = excluded.plan_sha256, routes_v4 = excluded.routes_v4, routes_v6 = excluded.routes_v6,
  raw_v4 = excluded.raw_v4, raw_v6 = excluded.raw_v6, extra_ratio_v4 = excluded.extra_ratio_v4, extra_ratio_v6 = excluded.extra_ratio_v6, warnings = excluded.warnings
"""
_INSERT_ROUTE = """
INSERT INTO plan_routes (cidr, family, first_bin, last_bin, kind, global_route_id, added_at, reconnected) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
"""
_INSERT_PART = "INSERT INTO plan_route_parts (route_id, cidr, first_bin, last_bin) VALUES (?, ?, ?, ?)"
_RECONNECT = """
INSERT INTO reconnect_state (id, pending_mode, pending_since, presence_until, last_reconnect_at) VALUES (1, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
  pending_mode = excluded.pending_mode, pending_since = excluded.pending_since, presence_until = excluded.presence_until, last_reconnect_at = excluded.last_reconnect_at
"""
_INSERT_RUN = """
INSERT INTO apply_runs (
  trigger, started_at, finished_at, routes_v4, routes_v6, routes_added, routes_removed, diff, file_sha256, file_changed, reconnect,
  clients_total, clients_reconnected, clients_failed, status, error
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
"""
_SELECT_RUN = """
SELECT id, trigger, started_at, finished_at, routes_v4, routes_v6, routes_added, routes_removed, diff, file_sha256, file_changed, reconnect,
       clients_total, clients_reconnected, clients_failed, status, error
FROM apply_runs
"""
_INSERT_CLIENT = """
INSERT INTO ovpn_clients (instance, cid, common_name, real_address, virtual_v4, virtual_v6, connected_at, snapshot_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


# The leading "plan" line keeps this distinct from routes_hash, including for an empty plan.
def plan_sha256(plan: RoutePlan) -> str:
    lines = []
    for route in sorted(plan.routes, key=route_order):
        link = "-" if route.global_route_id is None else str(route.global_route_id)
        lines.append(f"{route.network} {route.kind} {link}")
        lines.extend(f" {part.network}" for part in sorted(route.parts, key=lambda part: network_order(part.network)))
    return sha256(f"plan\n{'\n'.join(lines)}".encode()).hexdigest()


# Wider than the piece when exclusions trimmed it: addresses outside the piece still explain the part.
def _part_bounds(part: Part) -> tuple[IPAddress, IPAddress]:
    origins = [ip_network((address.ip, source.prefix.length), strict=False) for address in part.addresses for source in address.sources]
    if not origins:
        return part.network.network_address, part.network.broadcast_address
    first = min(origins, key=lambda network: (network.version, int(network.network_address))).network_address
    last = max(origins, key=lambda network: (network.version, int(network.broadcast_address))).broadcast_address
    if first.version != last.version:
        raise ValueError("a route part mixes address families")
    return first, last


def _bound_bins(part: Part) -> tuple[bytes, bytes]:
    first, last = _part_bounds(part)
    return first.packed, last.packed


def _mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"expected an object, got {value!r}")
    return value


def _text(data: Mapping[str, JsonValue], field: str) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {value!r}")
    return value


def _number(data: Mapping[str, JsonValue], field: str) -> int:
    value = data[field]
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer, got {value!r}")
    return value


def _strings(data: Mapping[str, JsonValue], field: str) -> tuple[str, ...]:
    value = data[field]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings, got {value!r}")
    return tuple(item for item in value if isinstance(item, str))


def _encoded_warning(warning: RouteWarning) -> dict[str, JsonValue]:
    match warning:
        case PlanWarning(kind, network, exclusion):
            return {"kind": kind, "network": str(network), "exclusion": str(exclusion.network), "reason": exclusion.reason}
        case BudgetWarning(version, limit, routes):
            return {"kind": warning.kind, "version": version, "limit": limit, "routes": routes}
        case GlobalRouteWarning(global_route_id, network, server):
            return {"kind": warning.kind, "global_route_id": global_route_id, "network": str(network), "server": str(server)}


def _warning(value: JsonValue) -> RouteWarning:
    data = _mapping(value)
    kind = WarningKind(_text(data, "kind"))
    match kind:
        case WarningKind.EXCLUDED | WarningKind.TRIMMED:
            exclusion = Exclusion(ip_network(_text(data, "exclusion")), ExclusionReason(_text(data, "reason")))
            return PlanWarning(kind, ip_network(_text(data, "network")), exclusion)
        case WarningKind.OVER_BUDGET:
            return BudgetWarning(_number(data, "version"), _number(data, "limit"), _number(data, "routes"))
        case WarningKind.COVERS_SERVER:
            return GlobalRouteWarning(GlobalRouteId(_number(data, "global_route_id")), ip_network(_text(data, "network")), ip_address(_text(data, "server")))


def _warnings_json(warnings: tuple[RouteWarning, ...]) -> str:
    return json.dumps([_encoded_warning(warning) for warning in warnings], ensure_ascii=False)


def _warnings(text: str) -> tuple[RouteWarning, ...]:
    raw: JsonValue = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError(f"warnings must be a list, got {raw!r}")
    return tuple(_warning(item) for item in raw)


def _diff_json(diff: "RouteDiff") -> str:
    return json.dumps({"added": list(diff.added), "removed": list(diff.removed)}, ensure_ascii=False)


def _diff(text: str) -> "RouteDiff":
    data = _mapping(json.loads(text))
    return RouteDiff(_strings(data, "added"), _strings(data, "removed"))


def _optional_time(value: object) -> Timestamp | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected a timestamp, got {value!r}")
    return Timestamp(value)


@dataclass(frozen=True, slots=True)
class StoredPart:
    network: IPNetwork
    first: IPAddress
    last: IPAddress


@dataclass(frozen=True, slots=True)
class StoredRoute:
    id: PlanRouteId
    network: IPNetwork
    kind: RouteKind
    global_route_id: GlobalRouteId | None
    added_at: Timestamp
    reconnected: bool
    parts: tuple[StoredPart, ...]


@dataclass(frozen=True, slots=True)
class StoredPlan:
    computed_at: Timestamp
    plan_sha256: str
    stats: PlanStats
    warnings: tuple[RouteWarning, ...]
    routes: tuple[StoredRoute, ...]


@dataclass(frozen=True, slots=True)
class ReconnectState:
    pending_mode: PendingReconnect | None = None
    pending_since: Timestamp | None = None
    presence_until: Timestamp | None = None
    last_reconnect_at: Timestamp | None = None

    def __post_init__(self) -> None:
        if (self.pending_mode is None) != (self.pending_since is None):
            raise ValueError("pending_mode and pending_since are both set or both absent")


@dataclass(frozen=True, slots=True)
class RouteDiff:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NewApplyRun:
    trigger: ApplyTrigger
    started_at: Timestamp
    routes_v4: int
    routes_v6: int
    routes_added: int
    routes_removed: int
    diff: RouteDiff
    file_sha256: str
    file_changed: bool
    reconnect: ReconnectMode
    status: ApplyStatus
    finished_at: Timestamp | None = None
    clients_total: int | None = None
    clients_reconnected: int | None = None
    clients_failed: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyRun:
    id: ApplyRunId
    trigger: ApplyTrigger
    started_at: Timestamp
    finished_at: Timestamp | None
    routes_v4: int
    routes_v6: int
    routes_added: int
    routes_removed: int
    diff: RouteDiff
    file_sha256: str
    file_changed: bool
    reconnect: ReconnectMode
    clients_total: int | None
    clients_reconnected: int | None
    clients_failed: int | None
    status: ApplyStatus
    error: str | None


@dataclass(frozen=True, slots=True)
class OvpnClient:
    instance: str
    cid: int
    common_name: str
    real_address: str
    connected_at: Timestamp
    snapshot_at: Timestamp
    virtual_v4: str | None = None
    virtual_v6: str | None = None


def _stored_routes(conn: sqlite3.Connection) -> tuple[StoredRoute, ...]:
    parts: dict[int, list[StoredPart]] = defaultdict(list)
    for row in conn.execute("SELECT route_id, cidr, first_bin, last_bin FROM plan_route_parts"):
        parts[row["route_id"]].append(StoredPart(ip_network(row["cidr"]), ip_address(row["first_bin"]), ip_address(row["last_bin"])))
    routes = []
    for row in conn.execute("SELECT id, cidr, kind, global_route_id, added_at, reconnected FROM plan_routes"):
        link = None if row["global_route_id"] is None else GlobalRouteId(row["global_route_id"])
        own = tuple(sorted(parts[row["id"]], key=lambda part: network_order(part.network)))
        routes.append(StoredRoute(PlanRouteId(row["id"]), ip_network(row["cidr"]), RouteKind(row["kind"]), link, Timestamp(row["added_at"]), bool(row["reconnected"]), own))
    return tuple(sorted(routes, key=lambda route: network_order(route.network)))


def load_plan(conn: sqlite3.Connection) -> StoredPlan | None:
    row = conn.execute("SELECT computed_at, plan_sha256, routes_v4, routes_v6, raw_v4, raw_v6, extra_ratio_v4, extra_ratio_v6, warnings FROM plan_state WHERE id = 1").fetchone()
    if row is None:
        return None
    stats = PlanStats(row["raw_v4"], row["raw_v6"], row["routes_v4"], row["routes_v6"], row["extra_ratio_v4"], row["extra_ratio_v6"])
    return StoredPlan(Timestamp(row["computed_at"]), row["plan_sha256"], stats, _warnings(row["warnings"]), _stored_routes(conn))


def _write_state(conn: sqlite3.Connection, plan: RoutePlan, digest: str, now: Timestamp) -> None:
    stats = plan.stats
    conn.execute(_STATE, (now, digest, stats.routes_v4, stats.routes_v6, stats.networks_v4, stats.networks_v6, stats.extra_ratio_v4, stats.extra_ratio_v6, _warnings_json(plan.warnings)))


def _replace_routes(conn: sqlite3.Connection, plan: RoutePlan, now: Timestamp) -> None:
    previous = {row["cidr"]: (Timestamp(row["added_at"]), row["reconnected"]) for row in conn.execute("SELECT cidr, added_at, reconnected FROM plan_routes")}
    conn.execute("DELETE FROM plan_routes")
    for route in sorted(plan.routes, key=route_order):
        added_at, reconnected = previous.get(str(route.network), (now, 0))
        network = route.network
        bins = network.network_address.packed, network.broadcast_address.packed
        row = conn.execute(_INSERT_ROUTE, (str(network), network.version, *bins, route.kind, route.global_route_id, added_at, reconnected)).fetchone()
        route_id: int = row["id"]
        conn.executemany(_INSERT_PART, ((route_id, str(part.network), *_bound_bins(part)) for part in route.parts))


def save_plan(conn: sqlite3.Connection, plan: RoutePlan, now: Timestamp) -> bool:
    digest = plan_sha256(plan)
    with transaction(conn):
        row = conn.execute("SELECT plan_sha256 FROM plan_state WHERE id = 1").fetchone()
        changed = row is None or row["plan_sha256"] != digest
        _write_state(conn, plan, digest, now)
        if changed:
            _replace_routes(conn, plan, now)
        return changed


def unreconnected_routes(conn: sqlite3.Connection) -> int:
    count: int = conn.execute("SELECT count(*) FROM plan_routes WHERE reconnected = 0").fetchone()[0]
    return count


def mark_routes_reconnected(conn: sqlite3.Connection) -> int:
    with transaction(conn):
        return conn.execute("UPDATE plan_routes SET reconnected = 1 WHERE reconnected = 0").rowcount


def load_reconnect_state(conn: sqlite3.Connection) -> ReconnectState:
    row = conn.execute("SELECT pending_mode, pending_since, presence_until, last_reconnect_at FROM reconnect_state WHERE id = 1").fetchone()
    if row is None:
        return ReconnectState()
    mode = None if row["pending_mode"] is None else PendingReconnect(row["pending_mode"])
    return ReconnectState(mode, _optional_time(row["pending_since"]), _optional_time(row["presence_until"]), _optional_time(row["last_reconnect_at"]))


def save_reconnect_state(conn: sqlite3.Connection, state: ReconnectState) -> None:
    with transaction(conn):
        conn.execute(_RECONNECT, (state.pending_mode, state.pending_since, state.presence_until, state.last_reconnect_at))


def _optional_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer, got {value!r}")
    return value


def _apply_run(row: sqlite3.Row) -> ApplyRun:
    return ApplyRun(
        ApplyRunId(row["id"]),
        ApplyTrigger(row["trigger"]),
        Timestamp(row["started_at"]),
        _optional_time(row["finished_at"]),
        row["routes_v4"],
        row["routes_v6"],
        row["routes_added"],
        row["routes_removed"],
        _diff(row["diff"]),
        row["file_sha256"],
        bool(row["file_changed"]),
        ReconnectMode(row["reconnect"]),
        _optional_count(row["clients_total"]),
        _optional_count(row["clients_reconnected"]),
        _optional_count(row["clients_failed"]),
        ApplyStatus(row["status"]),
        row["error"],
    )


def record_apply_run(conn: sqlite3.Connection, run: NewApplyRun) -> ApplyRunId:
    with transaction(conn):
        row = conn.execute(
            _INSERT_RUN,
            (
                run.trigger,
                run.started_at,
                run.finished_at,
                run.routes_v4,
                run.routes_v6,
                run.routes_added,
                run.routes_removed,
                _diff_json(run.diff),
                run.file_sha256,
                int(run.file_changed),
                run.reconnect,
                run.clients_total,
                run.clients_reconnected,
                run.clients_failed,
                run.status,
                run.error,
            ),
        ).fetchone()
        return ApplyRunId(row["id"])


def list_apply_runs(conn: sqlite3.Connection, limit: int) -> tuple[ApplyRun, ...]:
    return tuple(_apply_run(row) for row in conn.execute(f"{_SELECT_RUN} ORDER BY id DESC LIMIT ?", (limit,)))


def prune_apply_runs(conn: sqlite3.Connection, cutoff: Timestamp) -> int:
    with transaction(conn):
        return conn.execute("DELETE FROM apply_runs WHERE started_at < ?", (cutoff,)).rowcount


def replace_ovpn_clients(conn: sqlite3.Connection, clients: Iterable[OvpnClient]) -> None:
    rows = tuple(clients)
    with transaction(conn):
        conn.execute("DELETE FROM ovpn_clients")
        conn.executemany(_INSERT_CLIENT, ((c.instance, c.cid, c.common_name, c.real_address, c.virtual_v4, c.virtual_v6, c.connected_at, c.snapshot_at) for c in rows))


def list_ovpn_clients(conn: sqlite3.Connection) -> tuple[OvpnClient, ...]:
    query = "SELECT instance, cid, common_name, real_address, virtual_v4, virtual_v6, connected_at, snapshot_at FROM ovpn_clients ORDER BY instance, cid"
    return tuple(
        OvpnClient(row["instance"], row["cid"], row["common_name"], row["real_address"], Timestamp(row["connected_at"]), Timestamp(row["snapshot_at"]), row["virtual_v4"], row["virtual_v6"])
        for row in conn.execute(query)
    )
