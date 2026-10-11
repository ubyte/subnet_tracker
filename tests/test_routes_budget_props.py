from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from itertools import combinations

from hypothesis import given
from hypothesis import strategies as st

from routes_world import World
from subnet_tracker.config import IPNetwork
from subnet_tracker.routes.budget import apply_budget
from subnet_tracker.routes.model import BudgetWarning, PlanSettings, Route, RoutePlan
from subnet_tracker.types import RouteKind

BASE = int(IPv4Address("1.0.0.0"))
offsets = st.integers(0, 2**16 - 1)


def network(offset: int, prefix: int) -> IPv4Network:
    return IPv4Network((BASE + offset, prefix), strict=False)


@dataclass(frozen=True, slots=True)
class Case:
    raw: RoutePlan
    settings: PlanSettings
    exclude: list[IPv4Network]
    world: World

    @property
    def result(self) -> RoutePlan:
        return apply_budget(self.raw, self.settings)


@st.composite
def cases(draw: st.DrawFn) -> Case:
    w = World()
    for i, (offset, prefix) in enumerate(draw(st.lists(st.tuples(offsets, st.integers(14, 32)), max_size=30))):
        w.site(f"d{i}.example", str(IPv4Address(BASE + offset)), prefix_v4=prefix)
    for net in sorted({network(o, p) for o, p in draw(st.lists(st.tuples(offsets, st.integers(20, 28)), max_size=2))}):
        w.global_route(str(net))
    exclude = [network(o, p) for o, p in draw(st.lists(st.tuples(offsets, st.integers(16, 32)), max_size=4))]
    settings = PlanSettings(exclude=tuple(exclude), max_v4=draw(st.integers(0, 12)), min_aggregate_prefix_v4=draw(st.integers(12, 24)))
    raw = w.plan(PlanSettings(exclude=tuple(exclude), max_v4=10**6))
    return Case(raw, settings, exclude, w)


def inside(a: IPNetwork, b: IPNetwork) -> bool:
    return a.version == b.version and int(b.network_address) <= int(a.network_address) and int(a.broadcast_address) <= int(b.broadcast_address)


def dns(plan: RoutePlan) -> list[Route]:
    return [r for r in plan.routes if r.kind != RouteKind.GLOBAL]


def globals_of(plan: RoutePlan) -> list[Route]:
    return [r for r in plan.routes if r.kind == RouteKind.GLOBAL]


def top(net: IPNetwork, case: Case) -> IPNetwork:
    supernets = (net.supernet(new_prefix=prefix) for prefix in range(case.settings.min_aggregate_prefix_v4, net.prefixlen))
    return next((s for s in supernets if not any(s.overlaps(e) for e in case.exclude)), net)


def minimum(case: Case) -> int:
    return len({top(r.network, case) for r in dns(case.raw)}) + len(globals_of(case.raw))


@given(cases())
def test_within_budget_whenever_feasible(case: Case) -> None:
    result = case.result
    warnings = [w for w in result.warnings if isinstance(w, BudgetWarning)]
    if minimum(case) <= case.settings.max_v4:
        assert len(result.routes) <= case.settings.max_v4
        assert warnings == []
    else:
        assert len(result.routes) == minimum(case)
        assert warnings == [BudgetWarning(4, case.settings.max_v4, minimum(case))]
    assert (result.stats.routes_v4, result.stats.routes_v6) == (len(result.routes), 0)


@given(cases())
def test_every_routed_network_stays_covered_with_its_parts(case: Case) -> None:
    result = case.result
    assert all(any(inside(r.network, n.network) for n in dns(result)) for r in dns(case.raw))
    assert [p for r in dns(result) for p in r.parts] == [p for r in dns(case.raw) for p in r.parts]
    assert all(inside(p.network, r.network) for r in dns(result) for p in r.parts)
    assert globals_of(result) == globals_of(case.raw)


@given(cases())
def test_new_routes_are_aggregated_within_limits(case: Case) -> None:
    old = {r.network for r in case.raw.routes}
    for route in case.result.routes:
        assert not any(route.network.overlaps(e) for e in case.exclude) or route.kind == RouteKind.GLOBAL
        if route.network not in old:
            assert route.kind == RouteKind.AGGREGATED
            assert route.network.prefixlen >= case.settings.min_aggregate_prefix_v4


@given(cases())
def test_dns_routes_are_disjoint(case: Case) -> None:
    assert not any(a.network.overlaps(b.network) for a, b in combinations(dns(case.result), 2))


@given(cases())
def test_idempotent_and_unchanged_within_budget(case: Case) -> None:
    result = case.result
    assert apply_budget(result, case.settings) == result
    if len(case.raw.routes) <= case.settings.max_v4:
        assert result is case.raw


@given(cases())
def test_plan_routes_applies_budget_deterministically(case: Case) -> None:
    assert case.world.plan(case.settings) == case.result
    if len(case.raw.routes) > case.settings.max_v4:
        assert apply_budget(RoutePlan(case.raw.routes[::-1], case.raw.stats, case.raw.warnings), case.settings) == case.result
