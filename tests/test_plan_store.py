import sqlite3
from dataclasses import replace
from hashlib import sha256
from ipaddress import IPv4Network, ip_address, ip_network

import pytest

from routes_world import NOW, World
from subnet_tracker.core.db import transaction
from subnet_tracker.core.plan_store import (
    ApplyRun,
    NewApplyRun,
    OvpnClient,
    ReconnectState,
    RouteDiff,
    StoredPart,
    StoredPlan,
    StoredRoute,
    list_apply_runs,
    list_ovpn_clients,
    load_plan,
    load_reconnect_state,
    mark_routes_reconnected,
    plan_sha256,
    prune_apply_runs,
    record_apply_run,
    replace_ovpn_clients,
    save_plan,
    save_reconnect_state,
    unreconnected_routes,
)
from subnet_tracker.routes.model import (
    AddressSource,
    BudgetWarning,
    Exclusion,
    ExclusionReason,
    GlobalRouteWarning,
    Part,
    PartAddress,
    PlanSettings,
    PlanStats,
    PlanWarning,
    Prefix,
    PrefixSource,
    Route,
    RoutePlan,
    WarningKind,
)
from subnet_tracker.routes.render import render_routes, routes_hash
from subnet_tracker.types import (
    AddressId,
    ApplyRunId,
    ApplyStatus,
    ApplyTrigger,
    DomainId,
    GlobalRouteId,
    NameId,
    PendingReconnect,
    PlanRouteId,
    ReconnectMode,
    ResolverId,
    RouteKind,
    Timestamp,
)

type Warnings = tuple[PlanWarning | BudgetWarning | GlobalRouteWarning, ...]


def dns(cidr: str) -> Route:
    network = ip_network(cidr)
    return Route(network, RouteKind.DNS, (Part(network, ()),))


def make_plan(routes: tuple[Route, ...] = (), *, raw_v4: int | None = None, raw_v6: int | None = None, extra_v4: float = 0.0, extra_v6: float = 0.0, warnings: Warnings = ()) -> RoutePlan:
    v4 = sum(r.network.version == 4 for r in routes)
    v6 = len(routes) - v4
    return RoutePlan(routes, PlanStats(v4 if raw_v4 is None else raw_v4, v6 if raw_v6 is None else raw_v6, v4, v6, extra_v4, extra_v6), warnings)


def stored(db: sqlite3.Connection) -> StoredPlan:
    plan = load_plan(db)
    assert plan is not None
    return plan


def part_of(network: str) -> StoredPart:
    net = ip_network(network)
    return StoredPart(net, net.network_address, net.broadcast_address)


def apply_run(started: Timestamp, *, error: str | None = None, clients: int | None = None) -> NewApplyRun:
    return NewApplyRun(
        ApplyTrigger.MANUAL,
        started,
        0,
        0,
        0,
        0,
        RouteDiff(("198.51.100.0/24",), ("192.0.2.0/24",)),
        "cd" * 32,
        False,
        ReconnectMode.AUTO,
        ApplyStatus.OK,
        finished_at=started,
        clients_total=clients,
        clients_reconnected=clients,
        clients_failed=clients,
        error=error,
    )


def test_plan_sha256_is_canonical_text() -> None:
    plan = make_plan(
        (
            Route(ip_network("2001:db8::/32"), RouteKind.GLOBAL, global_route_id=GlobalRouteId(4)),
            Route(ip_network("192.0.2.0/24"), RouteKind.AGGREGATED, (Part(ip_network("192.0.2.128/25"), ()), Part(ip_network("192.0.2.0/25"), ()))),
        )
    )
    text = "plan\n192.0.2.0/24 aggregated -\n 192.0.2.0/25\n 192.0.2.128/25\n2001:db8::/32 global 4"
    assert plan_sha256(plan) == sha256(text.encode()).hexdigest()
    assert plan_sha256(make_plan()) == sha256(b"plan\n").hexdigest()


def test_plan_sha256_ignores_order_stats_warnings_and_addresses() -> None:
    source = AddressSource(AddressId(1), NameId(1), DomainId(1), Prefix(23, PrefixSource.DEFAULT), (ResolverId(1),))
    addressed = Part(ip_network("192.0.2.0/25"), (PartAddress(ip_address("192.0.2.1"), (source,)),))
    bare = Part(ip_network("192.0.2.0/25"), ())
    base = make_plan((Route(ip_network("192.0.2.0/24"), RouteKind.AGGREGATED, (addressed, Part(ip_network("192.0.2.128/25"), ()))),))
    swapped = replace(base, routes=(Route(base.routes[0].network, RouteKind.AGGREGATED, base.routes[0].parts[::-1]),))
    revised = replace(base, stats=PlanStats(9, 8, 1, 0, 0.5, 0.25), warnings=(BudgetWarning(4, 1, 2),))
    without_addresses = replace(base, routes=(Route(base.routes[0].network, RouteKind.AGGREGATED, (bare, base.routes[0].parts[1])),))
    assert plan_sha256(base) == plan_sha256(swapped) == plan_sha256(revised) == plan_sha256(without_addresses)


def test_plan_sha256_is_not_the_file_hash() -> None:
    network = ip_network("192.0.2.0/24")
    dns_plan = make_plan((Route(network, RouteKind.DNS, (Part(network, ()),)),))
    global_plan = make_plan((Route(network, RouteKind.GLOBAL, global_route_id=GlobalRouteId(4)),))
    assert routes_hash(render_routes(dns_plan, NOW).text) == routes_hash(render_routes(global_plan, NOW).text)
    assert plan_sha256(dns_plan) != plan_sha256(global_plan)
    assert plan_sha256(make_plan()) != routes_hash(render_routes(make_plan(), NOW).text)


def test_empty_plan_stores_state_without_routes(db: sqlite3.Connection) -> None:
    assert load_plan(db) is None
    plan = make_plan()
    assert save_plan(db, plan, NOW) is True
    assert load_plan(db) == StoredPlan(NOW, plan_sha256(plan), plan.stats, (), ())
    assert (mark_routes_reconnected(db), unreconnected_routes(db)) == (0, 0)
    assert save_plan(db, plan, Timestamp(NOW + 1)) is False
    assert stored(db).computed_at == Timestamp(NOW + 1)


def test_routes_change_only_when_the_hash_changes(db: sqlite3.Connection) -> None:
    v4, v6 = dns("192.0.2.0/24"), dns("2001:db8::/32")
    plan = make_plan((v4, v6), raw_v4=5, raw_v6=7, extra_v4=0.25, extra_v6=0.5)
    assert save_plan(db, plan, NOW) is True
    assert load_plan(db) == StoredPlan(
        NOW,
        plan_sha256(plan),
        plan.stats,
        (),
        (
            StoredRoute(PlanRouteId(1), v4.network, RouteKind.DNS, None, NOW, False, (part_of("192.0.2.0/24"),)),
            StoredRoute(PlanRouteId(2), v6.network, RouteKind.DNS, None, NOW, False, (part_of("2001:db8::/32"),)),
        ),
    )
    assert db.execute("SELECT raw_v4, raw_v6, routes_v4, routes_v6 FROM plan_state").fetchone()[:] == (5, 7, 1, 1)
    assert db.execute("SELECT length(first_bin) AS n FROM plan_routes WHERE family = 6").fetchone()["n"] == 16
    revised = replace(plan, stats=PlanStats(9, 8, 1, 1, 1.5, 0.0), warnings=(BudgetWarning(6, 300, 301),))
    assert save_plan(db, revised, Timestamp(NOW + 3)) is False
    kept = stored(db)
    assert (kept.computed_at, kept.plan_sha256, kept.stats, kept.warnings) == (Timestamp(NOW + 3), plan_sha256(plan), revised.stats, revised.warnings)
    assert [(r.id, r.added_at, r.reconnected, r.parts) for r in kept.routes] == [
        (PlanRouteId(1), NOW, False, (part_of("192.0.2.0/24"),)),
        (PlanRouteId(2), NOW, False, (part_of("2001:db8::/32"),)),
    ]


def test_reconnected_follows_the_cidr(db: sqlite3.Connection) -> None:
    original = dns("192.0.2.0/24")
    save_plan(db, make_plan((original,)), NOW)
    assert unreconnected_routes(db) == 1
    assert mark_routes_reconnected(db) == 1
    assert (mark_routes_reconnected(db), unreconnected_routes(db)) == (0, 0)
    changed = Route(original.network, RouteKind.AGGREGATED, (Part(ip_network("192.0.2.0/25"), ()), Part(ip_network("192.0.2.128/25"), ())))
    added = dns("198.51.100.0/24")
    assert save_plan(db, make_plan((changed, added)), Timestamp(NOW + 5)) is True
    by_cidr = {str(r.network): r for r in stored(db).routes}
    assert (by_cidr["192.0.2.0/24"].added_at, by_cidr["192.0.2.0/24"].reconnected, by_cidr["192.0.2.0/24"].kind) == (NOW, True, RouteKind.AGGREGATED)
    assert (by_cidr["198.51.100.0/24"].added_at, by_cidr["198.51.100.0/24"].reconnected) == (Timestamp(NOW + 5), False)
    assert unreconnected_routes(db) == 1
    assert save_plan(db, make_plan((added,)), Timestamp(NOW + 6)) is True
    [left] = stored(db).routes
    assert (str(left.network), left.added_at, left.reconnected) == ("198.51.100.0/24", Timestamp(NOW + 5), False)


def test_trimmed_part_keeps_the_source_network(db: sqlite3.Connection) -> None:
    world = World()
    world.site("example.com", "1.2.3.4")
    plan = world.plan(PlanSettings(server_addresses=(ip_address("1.2.2.1"),)))
    save_plan(db, plan, NOW)
    loaded = stored(db)
    origin = IPv4Network("1.2.2.0/23")
    parts = [part for route in loaded.routes for part in route.parts]
    assert parts
    assert all((part.first, part.last) == (origin.network_address, origin.broadcast_address) for part in parts)
    assert any(ip_address("1.2.3.4") not in part.network for part in parts)
    assert all(isinstance(part.network, IPv4Network) and part.network.subnet_of(origin) and part.network != origin for part in parts)
    assert loaded.warnings == (PlanWarning(WarningKind.TRIMMED, origin, Exclusion(ip_network("1.2.2.1/32"), ExclusionReason.SERVER_ADDRESS)),)


def test_warnings_round_trip(db: sqlite3.Connection) -> None:
    warnings = (
        PlanWarning(WarningKind.EXCLUDED, ip_network("10.1.0.0/16"), Exclusion(ip_network("10.0.0.0/8"), ExclusionReason.BUILTIN)),
        PlanWarning(WarningKind.TRIMMED, ip_network("192.0.2.0/24"), Exclusion(ip_network("192.0.2.1/32"), ExclusionReason.SETTING)),
        BudgetWarning(4, 1000, 1001),
        GlobalRouteWarning(GlobalRouteId(2), ip_network("2001:db8::/32"), ip_address("2001:db8::1")),
    )
    save_plan(db, make_plan((dns("192.0.2.0/24"),), warnings=warnings), NOW)
    assert stored(db).warnings == warnings


def test_global_route_is_stored_without_parts(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO global_routes (cidr, family, created_at, updated_at) VALUES ('198.51.100.0/24', 4, 0, 0)")
    route = Route(ip_network("198.51.100.0/24"), RouteKind.GLOBAL, global_route_id=GlobalRouteId(1))
    save_plan(db, make_plan((route,)), NOW)
    [stored_route] = stored(db).routes
    assert (stored_route.kind, stored_route.global_route_id, stored_route.parts, stored_route.reconnected) == (RouteKind.GLOBAL, GlobalRouteId(1), (), False)


def test_unknown_global_route_keeps_the_previous_plan(db: sqlite3.Connection) -> None:
    save_plan(db, make_plan((dns("192.0.2.0/24"),)), NOW)
    bad = make_plan((Route(ip_network("198.51.100.0/24"), RouteKind.GLOBAL, global_route_id=GlobalRouteId(99)),))
    with pytest.raises(sqlite3.IntegrityError):
        save_plan(db, bad, Timestamp(NOW + 1))
    assert not db.in_transaction
    kept = stored(db)
    assert (kept.computed_at, [str(r.network) for r in kept.routes]) == (NOW, ["192.0.2.0/24"])


def test_reconnect_state_round_trip(db: sqlite3.Connection) -> None:
    assert load_reconnect_state(db) == ReconnectState()
    state = ReconnectState(PendingReconnect.ON_LEAVE, NOW, Timestamp(NOW + 30), Timestamp(NOW - 60))
    save_reconnect_state(db, state)
    assert load_reconnect_state(db) == state
    cleared = ReconnectState(presence_until=Timestamp(NOW + 5), last_reconnect_at=Timestamp(NOW + 1))
    save_reconnect_state(db, cleared)
    assert load_reconnect_state(db) == cleared


def test_reconnect_state_needs_mode_and_since_together() -> None:
    with pytest.raises(ValueError, match="pending_mode and pending_since"):
        ReconnectState(pending_mode=PendingReconnect.AUTO)
    with pytest.raises(ValueError, match="pending_mode and pending_since"):
        ReconnectState(pending_since=NOW)


def test_apply_run_stores_the_file_hash(db: sqlite3.Connection) -> None:
    plan = make_plan((dns("192.0.2.0/24"), dns("2001:db8::/32")))
    rendered = render_routes(plan, NOW)
    diff = RouteDiff(("192.0.2.0/24", "2001:db8::/32"), ())
    recorded = NewApplyRun(ApplyTrigger.TICK, NOW, 1, 1, 2, 0, diff, rendered.sha256, True, ReconnectMode.ON_LEAVE, ApplyStatus.DEFERRED, finished_at=Timestamp(NOW + 2))
    assert record_apply_run(db, recorded) == ApplyRunId(1)
    [run] = list_apply_runs(db, 10)
    expected = ApplyRun(
        ApplyRunId(1),
        ApplyTrigger.TICK,
        NOW,
        Timestamp(NOW + 2),
        1,
        1,
        2,
        0,
        diff,
        routes_hash(rendered.text),
        True,
        ReconnectMode.ON_LEAVE,
        None,
        None,
        None,
        ApplyStatus.DEFERRED,
        None,
    )
    assert run == expected
    assert run.file_sha256 != plan_sha256(plan)


def test_apply_runs_prune_keeps_the_cutoff(db: sqlite3.Connection) -> None:
    for started in (NOW - 2, NOW - 1, NOW, NOW + 1):
        record_apply_run(db, apply_run(Timestamp(started), error="x" if started == NOW else None, clients=0 if started == NOW else None))
    assert [r.started_at for r in list_apply_runs(db, 1)] == [Timestamp(NOW + 1)]
    assert prune_apply_runs(db, NOW) == 2
    kept = list_apply_runs(db, 10)
    assert [r.started_at for r in kept] == [Timestamp(NOW + 1), NOW]
    at_cutoff = kept[1]
    assert (at_cutoff.error, at_cutoff.clients_total, at_cutoff.clients_reconnected, at_cutoff.clients_failed, at_cutoff.diff) == ("x", 0, 0, 0, RouteDiff(("198.51.100.0/24",), ("192.0.2.0/24",)))


def test_ovpn_clients_are_replaced_as_a_snapshot(db: sqlite3.Connection) -> None:
    first = OvpnClient("server", 2, "keenetic", "203.0.113.9:1194", NOW, NOW, virtual_v4="10.8.0.2")
    second = OvpnClient("server", 1, "phone", "2001:db8::5:1194", NOW, NOW, virtual_v6="fd00::2")
    later = OvpnClient("udp", 1, "laptop", "198.51.100.9:1194", Timestamp(NOW - 10), Timestamp(NOW + 1), "10.8.0.6", "fd00::6")
    replace_ovpn_clients(db, [first, second])
    assert list_ovpn_clients(db) == (second, first)
    replace_ovpn_clients(db, [later])
    assert list_ovpn_clients(db) == (later,)
    replace_ovpn_clients(db, [])
    assert list_ovpn_clients(db) == ()


def test_rejected_client_snapshot_keeps_the_previous_one(db: sqlite3.Connection) -> None:
    keep = OvpnClient("server", 1, "phone", "203.0.113.5:1194", NOW, NOW)
    replace_ovpn_clients(db, [keep])
    with pytest.raises(sqlite3.IntegrityError):
        replace_ovpn_clients(db, [keep, OvpnClient("server", 1, "other", "203.0.113.6:1194", NOW, NOW)])
    assert list_ovpn_clients(db) == (keep,)
    assert not db.in_transaction


def _write_and_fail(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    with transaction(db):
        save_plan(db, make_plan((dns("192.0.2.0/24"),)), NOW)
        save_reconnect_state(db, ReconnectState(PendingReconnect.AUTO, NOW))
        record_apply_run(db, apply_run(NOW))
        replace_ovpn_clients(db, [OvpnClient("server", 1, "phone", "203.0.113.5:1194", NOW, NOW)])
        assert load_plan(other) is None
        assert load_reconnect_state(other) == ReconnectState()
        assert list_apply_runs(other, 10) == ()
        assert list_ovpn_clients(other) == ()
        raise RuntimeError


def test_store_joins_the_outer_transaction(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        _write_and_fail(db, other)
    assert load_plan(db) is None
    assert load_reconnect_state(db) == ReconnectState()
    assert list_apply_runs(db, 10) == ()
    assert list_ovpn_clients(db) == ()
    assert not db.in_transaction
    with transaction(db):
        save_plan(db, make_plan((dns("192.0.2.0/24"),)), NOW)
        assert load_plan(other) is None
    assert load_plan(other) is not None
