from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from fractions import Fraction
from heapq import heappop, heappush
from ipaddress import IPv4Network, IPv6Network, collapse_addresses
from itertools import pairwise

from subnet_tracker.config import IPNetwork
from subnet_tracker.routes.exclusions import exclusions, family_holes, overlapping
from subnet_tracker.routes.model import BudgetWarning, Exclusion, PlanSettings, Route, RoutePlan, route_order
from subnet_tracker.types import RouteKind


def _common_supernet[N: (IPv4Network, IPv6Network)](a: N, b: N) -> N:
    return a.supernet(new_prefix=a.max_prefixlen - (int(a.network_address) ^ int(b.network_address)).bit_length())


def _aggregate[N: (IPv4Network, IPv6Network)](family: type[N], routes: Sequence[Route], budget: int, holes: Sequence[tuple[N, Exclusion]], min_prefix: int) -> list[Route]:
    by_network: dict[IPNetwork, Route] = {r.network: r for r in routes}
    nets = sorted(n for r in routes if isinstance(n := r.network, family))
    sizes = [n.num_addresses for n in nets]
    # (uncovered addresses, routes saved) of exactly the allowed smallest common supernets of current neighbours; a merge only shrinks the counts of its ancestors
    live: dict[N, tuple[int, int]] = {}
    heap: list[tuple[Fraction, int, int, int, int, N]] = []

    def span(s: N) -> slice:
        return slice(bisect_left(nets, s.network_address, key=lambda n: n.network_address), bisect_right(nets, s.broadcast_address, key=lambda n: n.network_address))

    def push(s: N, cost: int, gain: int) -> None:
        live[s] = cost, gain
        heappush(heap, (Fraction(cost, gain), -s.prefixlen, int(s.network_address), cost, gain, s))

    def consider(a: N, b: N) -> None:
        s = _common_supernet(a, b)
        if s not in live and s.prefixlen >= min_prefix and not overlapping(s, holes):
            inside = span(s)
            push(s, s.num_addresses - sum(sizes[inside]), inside.stop - inside.start - 1)

    for a, b in pairwise(nets):
        consider(a, b)
    while len(nets) > budget and heap:
        *_, cost, gain, s = heappop(heap)
        if live.get(s) != (cost, gain):
            continue
        inside = span(s)
        merged = nets[inside]
        for a, b in pairwise(merged):
            live.pop(_common_supernet(a, b), None)
        nets[inside], sizes[inside] = [s], [s.num_addresses]
        by_network[s] = Route(s, RouteKind.AGGREGATED, tuple(p for n in merged for p in by_network.pop(n).parts))
        for prefix in range(s.prefixlen - 1, min_prefix - 1, -1):
            if (ancestor := s.supernet(new_prefix=prefix)) in live:
                ancestor_cost, ancestor_gain = live[ancestor]
                push(ancestor, ancestor_cost - cost, ancestor_gain - gain)
        for neighbour in nets[max(inside.start - 1, 0) : inside.start] + nets[inside.start + 1 : inside.start + 2]:
            consider(neighbour, s)
    return [by_network[n] for n in nets]


def _within_budget[N: (IPv4Network, IPv6Network)](family: type[N], routes: Iterable[Route], excluded: Iterable[Exclusion], settings: PlanSettings) -> tuple[list[Route], list[BudgetWarning]]:
    version = 4 if family is IPv4Network else 6
    limit = settings.budget(version)
    own = [r for r in routes if isinstance(r.network, family)]
    if len(own) <= limit:
        return own, []
    global_routes = [r for r in own if r.kind == RouteKind.GLOBAL]
    dns = [r for r in own if r.kind != RouteKind.GLOBAL]
    kept = global_routes + _aggregate(family, dns, limit - len(global_routes), family_holes(family, excluded), settings.min_aggregate_prefix(version))
    return kept, [BudgetWarning(version, limit, len(kept))] if len(kept) > limit else []


def extra_ratio[N: (IPv4Network, IPv6Network)](family: type[N], routes: Iterable[Route]) -> float:
    dns = [r for r in routes if r.kind != RouteKind.GLOBAL and isinstance(r.network, family)]
    parts = [n for r in dns for p in r.parts if isinstance(n := p.network, family)]
    covered = sum(n.num_addresses for n in collapse_addresses(parts))
    return (sum(r.network.num_addresses for r in dns) - covered) / covered if covered else 0.0


def apply_budget(plan: RoutePlan, settings: PlanSettings) -> RoutePlan:
    counts = Counter(r.network.version for r in plan.routes)
    if all(counts[version] <= settings.budget(version) for version in (4, 6)):
        return plan
    excluded = exclusions(settings)
    routes_v4, warnings_v4 = _within_budget(IPv4Network, plan.routes, excluded, settings)
    routes_v6, warnings_v6 = _within_budget(IPv6Network, plan.routes, excluded, settings)
    routes = sorted([*routes_v4, *routes_v6], key=route_order)
    stats = replace(plan.stats, routes_v4=len(routes_v4), routes_v6=len(routes_v6), extra_ratio_v4=extra_ratio(IPv4Network, routes), extra_ratio_v6=extra_ratio(IPv6Network, routes))
    warnings = [w for w in plan.warnings if not isinstance(w, BudgetWarning)]
    return RoutePlan(tuple(routes), stats, (*warnings, *warnings_v4, *warnings_v6))
