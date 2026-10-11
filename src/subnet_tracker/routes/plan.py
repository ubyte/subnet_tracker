from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network, collapse_addresses, ip_network

from subnet_tracker.config import IPNetwork
from subnet_tracker.types import AddressId, DomainId, GlobalRouteId, GroupId, IPAddress, NameId, ResolverId, RouteKind, Seconds, Timestamp


@dataclass(frozen=True, slots=True)
class Group:
    id: GroupId
    enabled: bool = True
    prefix_v4: int | None = None
    prefix_v6: int | None = None


@dataclass(frozen=True, slots=True)
class Domain:
    id: DomainId
    enabled: bool = True
    groups: frozenset[GroupId] = frozenset()
    prefix_v4: int | None = None
    prefix_v6: int | None = None


@dataclass(frozen=True, slots=True)
class Name:
    id: NameId
    domain_id: DomainId


@dataclass(frozen=True, slots=True)
class Resolver:
    id: ResolverId
    use_for_routes: bool
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class Address:
    id: AddressId
    name_id: NameId
    ip: IPAddress


@dataclass(frozen=True, slots=True)
class Sighting:
    address_id: AddressId
    resolver_id: ResolverId
    last_seen_at: Timestamp
    answered_at: Timestamp  # name_resolves.answered_at of the address's name and this resolver


@dataclass(frozen=True, slots=True)
class GlobalRoute:
    id: GlobalRouteId
    network: IPNetwork
    enabled: bool = True
    groups: frozenset[GroupId] = frozenset()


@dataclass(frozen=True, slots=True)
class Snapshot:
    domains: tuple[Domain, ...] = ()
    groups: tuple[Group, ...] = ()
    names: tuple[Name, ...] = ()
    resolvers: tuple[Resolver, ...] = ()
    addresses: tuple[Address, ...] = ()
    sightings: tuple[Sighting, ...] = ()
    global_routes: tuple[GlobalRoute, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanSettings:
    retention: Seconds = Seconds(4 * 86400)
    default_prefix_v4: int = 23
    default_prefix_v6: int = 48
    push_ipv4: bool = True
    push_ipv6: bool = True
    exclude: tuple[IPNetwork, ...] = ()
    server_addresses: tuple[IPAddress, ...] = ()
    tunnel_networks: tuple[IPNetwork, ...] = ()

    def pushes(self, version: int) -> bool:
        return self.push_ipv4 if version == 4 else self.push_ipv6


class PrefixSource(StrEnum):
    DOMAIN = "domain"
    GROUP = "group"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class Prefix:
    length: int
    source: PrefixSource
    group_id: GroupId | None = None


class ExclusionReason(StrEnum):
    BUILTIN = "builtin"
    SERVER_ADDRESS = "server_address"
    TUNNEL_NETWORK = "tunnel_network"
    SETTING = "setting"


@dataclass(frozen=True, slots=True)
class Exclusion:
    network: IPNetwork
    reason: ExclusionReason


_BUILTIN = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3", "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8")
BUILTIN_EXCLUSIONS = tuple(Exclusion(ip_network(n), ExclusionReason.BUILTIN) for n in _BUILTIN)


class WarningKind(StrEnum):
    EXCLUDED = "excluded"
    TRIMMED = "trimmed"


@dataclass(frozen=True, slots=True)
class PlanWarning:
    kind: WarningKind
    network: IPNetwork
    exclusion: Exclusion

    @property
    def message(self) -> str:
        action = "dropped, inside" if self.kind == WarningKind.EXCLUDED else "trimmed around"
        return f"{self.network} {action} excluded {self.exclusion.network} ({self.exclusion.reason.replace('_', ' ')})"


@dataclass(frozen=True, slots=True)
class AddressSource:
    address_id: AddressId
    name_id: NameId
    domain_id: DomainId
    prefix: Prefix
    resolvers: tuple[ResolverId, ...]


@dataclass(frozen=True, slots=True)
class PartAddress:
    ip: IPAddress
    sources: tuple[AddressSource, ...]

    @property
    def names(self) -> tuple[NameId, ...]:
        return tuple(s.name_id for s in self.sources)

    @property
    def resolvers(self) -> tuple[ResolverId, ...]:
        return tuple(sorted({r for s in self.sources for r in s.resolvers}))


@dataclass(frozen=True, slots=True)
class Part:
    network: IPNetwork
    addresses: tuple[PartAddress, ...]  # all addresses of the network the part was trimmed from, some may lie outside it


@dataclass(frozen=True, slots=True)
class Route:
    network: IPNetwork
    kind: RouteKind
    parts: tuple[Part, ...] = ()
    global_route_id: GlobalRouteId | None = None


@dataclass(frozen=True, slots=True)
class PlanStats:
    networks_v4: int
    networks_v6: int
    routes_v4: int
    routes_v6: int


@dataclass(frozen=True, slots=True)
class RoutePlan:
    routes: tuple[Route, ...]
    stats: PlanStats
    warnings: tuple[PlanWarning, ...]


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


def exclusions(settings: PlanSettings) -> tuple[Exclusion, ...]:
    return (
        *BUILTIN_EXCLUSIONS,
        *(Exclusion(ip_network(ip), ExclusionReason.SERVER_ADDRESS) for ip in settings.server_addresses),
        *(Exclusion(n, ExclusionReason.TUNNEL_NETWORK) for n in settings.tunnel_networks),
        *(Exclusion(n, ExclusionReason.SETTING) for n in settings.exclude),
    )


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


def _cut[N: (IPv4Network, IPv6Network)](piece: N, hole: N) -> Iterable[N]:
    if not piece.overlaps(hole):
        return (piece,)
    return () if piece.subnet_of(hole) else piece.address_exclude(hole)


def _trim[N: (IPv4Network, IPv6Network)](network: N, holes: Iterable[N]) -> list[N]:
    pieces = [network]
    for hole in holes:
        pieces = [q for p in pieces for q in _cut(p, hole)]
    return pieces


def _warnings[N: (IPv4Network, IPv6Network)](network: N, holes: Sequence[tuple[N, Exclusion]]) -> list[PlanWarning]:
    if (inside := next((e for n, e in holes if network.subnet_of(n)), None)) is not None:
        return [PlanWarning(WarningKind.EXCLUDED, network, inside)]
    return [PlanWarning(WarningKind.TRIMMED, network, e) for n, e in holes if network.overlaps(n)]


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
    holes = [(n, e) for e in excluded if isinstance(n := e.network, family)]
    covering = [n for r in global_routes if isinstance(n := r.network, family)]
    origins: defaultdict[N, list[Fed]] = defaultdict(list)
    for ip, source in fed:
        if ip.version == version:
            origins[family((ip, source.prefix.length), strict=False)].append((ip, source))
    parts: defaultdict[N, list[Fed]] = defaultdict(list)
    warnings = []
    for origin in sorted(origins):
        warnings += _warnings(origin, holes)
        for piece in _trim(origin, (n for n, _ in holes)):
            if not any(piece.subnet_of(g) for g in covering):
                parts[piece] += origins[origin]
    return list(_collapse(parts)), warnings, len(parts)


def _order(route: Route) -> tuple[int, int, int]:
    return route.network.version, int(route.network.network_address), route.network.prefixlen


def plan_routes(snapshot: Snapshot, settings: PlanSettings) -> RoutePlan:
    groups = {g.id: g for g in snapshot.groups}
    fed = _fed_addresses(snapshot, groups, settings)
    excluded = exclusions(settings)
    global_routes = [r for r in snapshot.global_routes if settings.pushes(r.network.version) and routable(r.enabled, r.groups, groups)]
    routes_v4, warnings_v4, networks_v4 = _dns_routes(IPv4Network, fed, excluded, global_routes)
    routes_v6, warnings_v6, networks_v6 = _dns_routes(IPv6Network, fed, excluded, global_routes)
    routes = sorted([*routes_v4, *routes_v6, *(Route(r.network, RouteKind.GLOBAL, global_route_id=r.id) for r in global_routes)], key=_order)
    stats = PlanStats(networks_v4, networks_v6, sum(r.network.version == 4 for r in routes), sum(r.network.version == 6 for r in routes))
    return RoutePlan(tuple(routes), stats, (*warnings_v4, *warnings_v6))
