from collections.abc import Iterable
from ipaddress import IPv4Address, IPv4Network
from itertools import combinations

from hypothesis import given
from hypothesis import strategies as st

from routes_world import World
from subnet_tracker.config import IPNetwork
from subnet_tracker.routes.model import PlanSettings, RoutePlan
from subnet_tracker.types import RouteKind

type Interval = tuple[int, int]

BASE = int(IPv4Address("1.0.0.0"))
offsets = st.integers(0, 2**16 - 1)
networks = st.lists(st.tuples(offsets, st.integers(20, 32)), max_size=25)
excludes = st.lists(st.tuples(offsets, st.integers(18, 32)), max_size=4)


def interval(network: IPNetwork) -> Interval:
    return int(network.network_address), int(network.broadcast_address)


def union(intervals: Iterable[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for first, last in sorted(intervals):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def minus(intervals: list[Interval], holes: list[Interval]) -> list[Interval]:
    result = []
    for first, last in intervals:
        for hole_first, hole_last in holes:
            if hole_last < first or hole_first > last:
                continue
            if hole_first > first:
                result.append((first, hole_first - 1))
            first = hole_last + 1
        if first <= last:
            result.append((first, last))
    return union(result)


def network(offset: int, prefix: int) -> IPv4Network:
    return IPv4Network((BASE + offset, prefix), strict=False)


def plan_of(nets: list[tuple[int, int]], excl: list[tuple[int, int]]) -> tuple[RoutePlan, list[IPv4Network]]:
    w = World()
    for i, (offset, prefix) in enumerate(nets):
        w.site(f"d{i}.example", str(IPv4Address(BASE + offset)), prefix_v4=prefix)
    exclude = [network(o, p) for o, p in excl]
    return w.plan(PlanSettings(exclude=tuple(exclude))), exclude


@given(networks, excludes)
def test_routes_cover_exactly_networks_minus_exclusions(nets: list[tuple[int, int]], excl: list[tuple[int, int]]) -> None:
    plan, exclude = plan_of(nets, excl)
    expected = minus(union(interval(network(o, p)) for o, p in nets), union(map(interval, exclude)))
    assert union(interval(r.network) for r in plan.routes) == expected


@given(networks, excludes)
def test_routes_are_disjoint_and_maximal(nets: list[tuple[int, int]], excl: list[tuple[int, int]]) -> None:
    plan, _ = plan_of(nets, excl)
    routes = {r.network for r in plan.routes}
    assert not any(a.overlaps(b) for a, b in combinations(routes, 2))
    for route in routes:
        [sibling] = (n for n in route.supernet().subnets() if n != route)
        assert sibling not in routes


@given(networks, excludes)
def test_parts_partition_their_route(nets: list[tuple[int, int]], excl: list[tuple[int, int]]) -> None:
    plan, exclude = plan_of(nets, excl)
    for route in plan.routes:
        assert route.parts
        assert union(interval(p.network) for p in route.parts) == [interval(route.network)]
        assert all(not p.network.overlaps(e) for p in route.parts for e in exclude)
        assert (route.kind == RouteKind.DNS) == ([p.network for p in route.parts] == [route.network])


@given(networks, excludes)
def test_every_address_outside_exclusions_is_routed_with_provenance(nets: list[tuple[int, int]], excl: list[tuple[int, int]]) -> None:
    plan, exclude = plan_of(nets, excl)
    for offset, _ in nets:
        ip = IPv4Address(BASE + offset)
        if any(ip in e for e in exclude):
            continue
        assert any(ip in p.network and ip in {a.ip for a in p.addresses} for r in plan.routes for p in r.parts)
