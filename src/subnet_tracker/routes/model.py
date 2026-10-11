from dataclasses import dataclass
from enum import StrEnum

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
