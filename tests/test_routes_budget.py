from ipaddress import IPv4Network, ip_address, ip_network

import pytest

from routes_world import World, cidrs
from subnet_tracker.routes.budget import apply_budget
from subnet_tracker.routes.model import BudgetWarning, PlanSettings, RoutePlan, WarningKind
from subnet_tracker.types import RouteKind

UNLIMITED = PlanSettings(max_v4=10**6, max_v6=10**6)


def sites(*cidrs: str) -> World:
    w = World()
    for i, cidr in enumerate(cidrs):
        network = ip_network(cidr)
        w.site(f"d{i}.example", str(network.network_address), prefix_v4=network.prefixlen, prefix_v6=network.prefixlen)
    return w


def budget_warnings(plan: RoutePlan) -> list[BudgetWarning]:
    return [w for w in plan.warnings if isinstance(w, BudgetWarning)]


def test_plan_within_budget_is_unchanged() -> None:
    raw = sites("1.0.0.0/24", "1.0.2.0/24", "2001:db8:1::/48").plan(UNLIMITED)
    assert apply_budget(raw, PlanSettings(max_v4=2, max_v6=1)) is raw


@pytest.mark.parametrize(
    ("budget", "expected", "extra"),
    [
        (5, ["1.0.0.0/24", "1.0.2.0/24", "2.0.0.0/24", "2.0.2.0/24", "2.0.3.0/25"], 0),
        (4, ["1.0.0.0/24", "1.0.2.0/24", "2.0.0.0/24", "2.0.2.0/23"], 128),
        (3, ["1.0.0.0/24", "1.0.2.0/24", "2.0.0.0/22"], 384),
        (2, ["1.0.0.0/22", "2.0.0.0/22"], 896),
    ],
)
def test_merges_with_least_extra_space(budget: int, expected: list[str], extra: int) -> None:
    plan = sites("1.0.0.0/24", "1.0.2.0/24", "2.0.0.0/24", "2.0.2.0/24", "2.0.3.0/25").plan(PlanSettings(max_v4=budget))
    assert cidrs(plan) == expected
    covered = 4 * 256 + 128
    assert plan.stats.extra_ratio_v4 == pytest.approx(extra / covered)
    assert budget_warnings(plan) == []


def test_cost_is_per_route_saved() -> None:
    plan = sites("3.0.0.0/21", "3.0.8.0/23", "3.0.12.0/23", "4.0.0.0/25", "4.0.2.0/24").plan(PlanSettings(max_v4=4))
    assert cidrs(plan) == ["3.0.0.0/20", "4.0.0.0/25", "4.0.2.0/24"]


@pytest.mark.parametrize(("exclude", "expected"), [((), ["1.0.0.0/22", "1.0.8.0/24", "1.0.10.0/24"]), ((ip_network("1.0.1.0/24"),), ["1.0.0.0/24", "1.0.2.0/24", "1.0.8.0/22"])])
def test_equal_cost_takes_lower_address_unless_excluded(exclude: tuple[IPv4Network, ...], expected: list[str]) -> None:
    plan = sites("1.0.0.0/24", "1.0.2.0/24", "1.0.8.0/24", "1.0.10.0/24").plan(PlanSettings(max_v4=3, exclude=exclude))
    assert cidrs(plan) == expected


def test_aggregated_route_keeps_parts_of_merged_routes() -> None:
    w = sites("1.0.0.0/24", "1.0.2.0/25", "1.0.2.128/25")
    raw = w.plan(UNLIMITED)
    [route] = w.plan(PlanSettings(max_v4=1)).routes
    assert (str(route.network), route.kind) == ("1.0.0.0/22", RouteKind.AGGREGATED)
    assert route.parts == tuple(p for r in raw.routes for p in r.parts)
    assert [str(p.network) for p in route.parts] == ["1.0.0.0/24", "1.0.2.0/25", "1.0.2.128/25"]
    assert [str(a.ip) for p in route.parts for a in p.addresses] == ["1.0.0.0", "1.0.2.0", "1.0.2.128"]


def test_stats_count_routes_after_budget() -> None:
    plan = sites("1.0.0.0/24", "1.0.2.0/24", "2001:db8:1::/48").plan(PlanSettings(max_v4=1))
    assert (plan.stats.networks_v4, plan.stats.routes_v4, plan.stats.routes_v6) == (2, 1, 1)
    assert (plan.stats.extra_ratio_v4, plan.stats.extra_ratio_v6) == (1.0, 0.0)


def test_supernet_overlapping_exclusion_is_not_used() -> None:
    w = sites("1.0.0.0/24", "1.0.2.0/24")
    plan = w.plan(PlanSettings(max_v4=1, exclude=(ip_network("1.0.1.0/24"),)))
    assert cidrs(plan) == ["1.0.0.0/24", "1.0.2.0/24"]
    assert budget_warnings(plan) == [BudgetWarning(4, 1, 2)]


def test_server_address_blocks_supernet() -> None:
    plan = sites("1.0.0.0/24", "1.0.2.0/24").plan(PlanSettings(max_v4=1, server_addresses=(ip_address("1.0.3.1"),)))
    assert cidrs(plan) == ["1.0.0.0/24", "1.0.2.0/24"]


def test_exclusion_outside_supernet_does_not_block() -> None:
    plan = sites("1.0.0.0/24", "1.0.2.0/24").plan(PlanSettings(max_v4=1, exclude=(ip_network("1.0.4.0/24"),)))
    assert cidrs(plan) == ["1.0.0.0/22"]


@pytest.mark.parametrize(("min_prefix", "expected"), [(12, ["1.0.0.0/24", "1.128.0.0/24"]), (8, ["1.0.0.0/8"])])
def test_min_aggregate_prefix(min_prefix: int, expected: list[str]) -> None:
    plan = sites("1.0.0.0/24", "1.128.0.0/24").plan(PlanSettings(max_v4=1, min_aggregate_prefix_v4=min_prefix))
    assert cidrs(plan) == expected


def test_short_network_from_dns_is_kept() -> None:
    plan = sites("1.0.0.0/10", "1.128.0.0/24", "1.128.2.0/24").plan(PlanSettings(max_v4=2))
    assert cidrs(plan) == ["1.0.0.0/10", "1.128.0.0/22"]


def test_ipv6_budget_and_min_prefix() -> None:
    plan = sites("2001:db8:1::/48", "2001:db8:5::/48", "2001:db9::/48").plan(PlanSettings(max_v6=1))
    assert cidrs(plan) == ["2001:db8::/45", "2001:db9::/48"]
    assert budget_warnings(plan) == [BudgetWarning(6, 1, 2)]


def test_families_have_separate_budgets() -> None:
    plan = sites("1.0.0.0/24", "1.0.2.0/24", "2001:db8:1::/48", "2001:db8:5::/48").plan(PlanSettings(max_v4=1, max_v6=2))
    assert cidrs(plan) == ["1.0.0.0/22", "2001:db8:1::/48", "2001:db8:5::/48"]


def test_global_routes_take_budget() -> None:
    w = sites("1.0.0.0/24", "1.0.2.0/24")
    w.global_route("149.154.160.0/20")
    assert cidrs(w.plan(PlanSettings(max_v4=3))) == ["1.0.0.0/24", "1.0.2.0/24", "149.154.160.0/20"]
    assert cidrs(w.plan(PlanSettings(max_v4=2))) == ["1.0.0.0/22", "149.154.160.0/20"]
    plan = w.plan(PlanSettings(max_v4=1))
    assert cidrs(plan) == ["1.0.0.0/22", "149.154.160.0/20"]
    assert budget_warnings(plan) == [BudgetWarning(4, 1, 2)]


def test_supernet_may_cover_global_route() -> None:
    w = sites("1.0.0.0/24", "1.0.3.0/24")
    w.global_route("1.0.1.0/24")
    plan = w.plan(PlanSettings(max_v4=2))
    assert [(str(r.network), r.kind) for r in plan.routes] == [("1.0.0.0/22", RouteKind.AGGREGATED), ("1.0.1.0/24", RouteKind.GLOBAL)]


def test_budget_warning() -> None:
    warning = BudgetWarning(4, 1000, 1234)
    assert warning.kind == WarningKind.OVER_BUDGET
    assert warning.message == "IPv4 route budget 1000 is unreachable, at least 1234 routes"


def test_unreachable_budget_keeps_other_warnings_once() -> None:
    w = sites("1.0.0.0/24", "1.0.2.0/23")
    settings = PlanSettings(max_v4=1, exclude=(ip_network("1.0.1.0/24"), ip_network("1.0.3.0/24")))
    plan = w.plan(settings)
    assert [x.kind for x in plan.warnings] == [WarningKind.TRIMMED, WarningKind.OVER_BUDGET]
    assert apply_budget(plan, settings) == plan
