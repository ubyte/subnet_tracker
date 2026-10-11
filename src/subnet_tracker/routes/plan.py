from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from ipaddress import IPv4Network, IPv6Network, collapse_addresses, ip_network

from subnet_tracker.config import IPNetwork
from subnet_tracker.routes.exclusions import builtin_of, exclusions, family_holes, overlapping, trim
from subnet_tracker.routes.model import (
    AddressSource,
    Domain,
    Exclusion,
    GlobalRoute,
    Group,
    Part,
    PartAddress,
    PlanSettings,
    PlanStats,
    PlanWarning,
    Prefix,
    PrefixSource,
    Resolver,
    Route,
    RoutePlan,
    Sighting,
    Snapshot,
    WarningKind,
)
from subnet_tracker.types import AddressId, GroupId, IPAddress, ResolverId, RouteKind, Seconds

type Fed = tuple[IPAddress, AddressSource]


def routable(enabled: bool, groups: frozenset[GroupId], all_groups: Mapping[GroupId, Group]) -> bool:
    return enabled and (not groups or any(all_groups[g].enabled for g in groups))


def feeds_routes(sighting: Sighting, resolver: Resolver, retention: Seconds) -> bool:
    return resolver.use_for_routes and not resolver.revoked and sighting.answered_at - sighting.last_seen_at < retention


def _of_family(version: int, v4: int | None, v6: int | None) -> int | None:
    return v4 if version == 4 else v6


def effective_prefix(domain: Domain, groups: Mapping[GroupId, Group], settings: PlanSettings, version: int) -> Prefix:
    if (own := _of_family(version, domain.prefix_v4, domain.prefix_v6)) is not None:
        return Prefix(own, PrefixSource.DOMAIN)
    inherited = [(length, g.id) for i in domain.groups if (g := groups[i]).enabled and (length := _of_family(version, g.prefix_v4, g.prefix_v6)) is not None]
    if inherited:
        length, group_id = min(inherited)
        return Prefix(length, PrefixSource.GROUP, group_id)
    return Prefix(settings.default_prefix_v4 if version == 4 else settings.default_prefix_v6, PrefixSource.DEFAULT)


def _fed_addresses(snapshot: Snapshot, groups: Mapping[GroupId, Group], settings: PlanSettings) -> list[Fed]:
    domains = {d.id: d for d in snapshot.domains if routable(d.enabled, d.groups, groups)}
    domain_of = {n.id: n.domain_id for n in snapshot.names}
    resolvers = {r.id: r for r in snapshot.resolvers}
    feeding: defaultdict[AddressId, set[ResolverId]] = defaultdict(set)
    for s in snapshot.sightings:
        if feeds_routes(s, resolvers[s.resolver_id], settings.retention):
            feeding[s.address_id].add(s.resolver_id)
    fed = []
    for a in snapshot.addresses:
        if a.id in feeding and settings.pushes(a.ip.version) and (domain := domains.get(domain_of[a.name_id])) is not None:
            fed.append((a.ip, AddressSource(a.id, a.name_id, domain.id, effective_prefix(domain, groups, settings, a.ip.version), tuple(sorted(feeding[a.id])))))
    return fed


def _drop_builtin(fed: Iterable[Fed]) -> tuple[list[Fed], list[PlanWarning]]:
    kept: list[Fed] = []
    dropped: dict[IPAddress, Exclusion] = {}
    for ip, source in fed:
        if (builtin := builtin_of(ip)) is None:
            kept.append((ip, source))
        else:
            dropped[ip] = builtin
    return kept, [PlanWarning(WarningKind.EXCLUDED, ip_network(ip), e) for ip, e in sorted(dropped.items(), key=lambda item: (item[0].version, int(item[0])))]


def _warnings[N: (IPv4Network, IPv6Network)](network: N, holes: Sequence[tuple[N, Exclusion]]) -> list[PlanWarning]:
    hit = overlapping(network, holes)
    if (inside := next((e for n, e in hit if network.subnet_of(n)), None)) is not None:
        return [PlanWarning(WarningKind.EXCLUDED, network, inside)]
    return [PlanWarning(WarningKind.TRIMMED, network, e) for _, e in hit]


def _part(network: IPNetwork, fed: Iterable[Fed]) -> Part:
    by_ip: defaultdict[IPAddress, list[AddressSource]] = defaultdict(list)
    for ip, source in fed:
        by_ip[ip].append(source)
    return Part(network, tuple(PartAddress(ip, tuple(sorted(sources, key=lambda s: (s.name_id, s.address_id)))) for ip, sources in sorted(by_ip.items(), key=lambda item: int(item[0]))))


def _collapse[N: (IPv4Network, IPv6Network)](parts: Mapping[N, list[Fed]]) -> Iterator[Route]:
    ordered = sorted(parts)
    start = 0
    for network in collapse_addresses(ordered):
        end = bisect_right(ordered, network.broadcast_address, lo=start, key=lambda p: p.network_address)
        inside, start = ordered[start:end], end
        kind = RouteKind.DNS if inside == [network] else RouteKind.AGGREGATED
        yield Route(network, kind, tuple(_part(p, parts[p]) for p in inside))


def _dns_routes[N: (IPv4Network, IPv6Network)](family: type[N], fed: Iterable[Fed], excluded: Iterable[Exclusion], global_routes: Iterable[GlobalRoute]) -> tuple[list[Route], list[PlanWarning], int]:
    version = 4 if family is IPv4Network else 6
    holes = family_holes(family, excluded)
    covering = [n for r in global_routes if isinstance(n := r.network, family)]
    origins: defaultdict[N, list[Fed]] = defaultdict(list)
    for ip, source in fed:
        if ip.version == version:
            origins[family((ip, source.prefix.length), strict=False)].append((ip, source))
    parts: defaultdict[N, list[Fed]] = defaultdict(list)
    warnings = []
    for origin in sorted(origins):
        warnings += _warnings(origin, holes)
        for piece in trim(origin, (n for n, _ in holes)):
            if not any(piece.subnet_of(g) for g in covering):
                parts[piece] += origins[origin]
    return list(_collapse(parts)), warnings, len(parts)


def _order(route: Route) -> tuple[int, int, int]:
    return route.network.version, int(route.network.network_address), route.network.prefixlen


def plan_routes(snapshot: Snapshot, settings: PlanSettings) -> RoutePlan:
    groups = {g.id: g for g in snapshot.groups}
    fed, dropped = _drop_builtin(_fed_addresses(snapshot, groups, settings))
    excluded = exclusions(settings)
    global_routes = [r for r in snapshot.global_routes if settings.pushes(r.network.version) and routable(r.enabled, r.groups, groups)]
    routes_v4, warnings_v4, networks_v4 = _dns_routes(IPv4Network, fed, excluded, global_routes)
    routes_v6, warnings_v6, networks_v6 = _dns_routes(IPv6Network, fed, excluded, global_routes)
    routes = sorted([*routes_v4, *routes_v6, *(Route(r.network, RouteKind.GLOBAL, global_route_id=r.id) for r in global_routes)], key=_order)
    stats = PlanStats(networks_v4, networks_v6, sum(r.network.version == 4 for r in routes), sum(r.network.version == 6 for r in routes))
    return RoutePlan(tuple(routes), stats, (*dropped, *warnings_v4, *warnings_v6))
