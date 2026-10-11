from ipaddress import ip_address, ip_network

from subnet_tracker.routes.model import Address, Domain, GlobalRoute, Group, Name, PlanSettings, Resolver, RoutePlan, Sighting, Snapshot
from subnet_tracker.routes.plan import plan_routes
from subnet_tracker.types import AddressId, DomainId, GlobalRouteId, GroupId, NameId, ResolverId, Timestamp

NOW = Timestamp(1_800_000_000)
DAY = 86400
SYSTEM = ResolverId(1)


class World:
    def __init__(self) -> None:
        self.domains: list[Domain] = []
        self.groups: list[Group] = []
        self.names: dict[str, Name] = {}
        self.resolvers = [Resolver(SYSTEM, use_for_routes=True)]
        self.addresses: list[Address] = []
        self.sightings: list[Sighting] = []
        self.global_routes: list[GlobalRoute] = []

    def group(self, *, enabled: bool = True, prefix_v4: int | None = None, prefix_v6: int | None = None) -> GroupId:
        gid = GroupId(len(self.groups) + 1)
        self.groups.append(Group(gid, enabled=enabled, prefix_v4=prefix_v4, prefix_v6=prefix_v6))
        return gid

    def resolver(self, *, use_for_routes: bool = True, revoked: bool = False) -> ResolverId:
        rid = ResolverId(len(self.resolvers) + 1)
        self.resolvers.append(Resolver(rid, use_for_routes=use_for_routes, revoked=revoked))
        return rid

    def domain(self, *names: str, enabled: bool = True, groups: tuple[GroupId, ...] = (), prefix_v4: int | None = None, prefix_v6: int | None = None) -> DomainId:
        did = DomainId(len(self.domains) + 1)
        self.domains.append(Domain(did, enabled=enabled, groups=frozenset(groups), prefix_v4=prefix_v4, prefix_v6=prefix_v6))
        for name in names:
            self.names[name] = Name(NameId(len(self.names) + 1), did)
        return did

    def address(self, name: str, ip: str, *, resolver: ResolverId = SYSTEM, last_seen_at: int = NOW, answered_at: int = NOW) -> AddressId:
        aid = AddressId(len(self.addresses) + 1)
        self.addresses.append(Address(aid, self.names[name].id, ip_address(ip)))
        self.sighting(aid, resolver, last_seen_at=last_seen_at, answered_at=answered_at)
        return aid

    def sighting(self, address: AddressId, resolver: ResolverId, *, last_seen_at: int = NOW, answered_at: int = NOW) -> None:
        self.sightings.append(Sighting(address, resolver, last_seen_at=Timestamp(last_seen_at), answered_at=Timestamp(answered_at)))

    def site(self, name: str, *ips: str, enabled: bool = True, groups: tuple[GroupId, ...] = (), prefix_v4: int | None = None, prefix_v6: int | None = None) -> DomainId:
        did = self.domain(name, enabled=enabled, groups=groups, prefix_v4=prefix_v4, prefix_v6=prefix_v6)
        for ip in ips:
            self.address(name, ip)
        return did

    def global_route(self, cidr: str, *, enabled: bool = True, groups: tuple[GroupId, ...] = ()) -> GlobalRouteId:
        rid = GlobalRouteId(len(self.global_routes) + 1)
        self.global_routes.append(GlobalRoute(rid, ip_network(cidr), enabled=enabled, groups=frozenset(groups)))
        return rid

    def snapshot(self) -> Snapshot:
        return Snapshot(tuple(self.domains), tuple(self.groups), tuple(self.names.values()), tuple(self.resolvers), tuple(self.addresses), tuple(self.sightings), tuple(self.global_routes))

    def plan(self, settings: PlanSettings | None = None) -> RoutePlan:
        return plan_routes(self.snapshot(), settings or PlanSettings())


def cidrs(plan: RoutePlan) -> list[str]:
    return [str(r.network) for r in plan.routes]
