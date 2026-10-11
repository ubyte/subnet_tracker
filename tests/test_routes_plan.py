from dataclasses import replace
from ipaddress import IPv4Network, ip_address, ip_network

import pytest

from routes_world import DAY, NOW, SYSTEM, World, cidrs
from subnet_tracker.routes.exclusions import BUILTIN_EXCLUSIONS, exclusions
from subnet_tracker.routes.model import ExclusionReason, PlanSettings, Prefix, PrefixSource, Route, Snapshot, WarningKind
from subnet_tracker.routes.plan import plan_routes
from subnet_tracker.types import RouteKind, Seconds


def only_route_of(snapshot: Snapshot, settings: PlanSettings | None = None) -> Route:
    [route] = plan_routes(snapshot, settings or PlanSettings()).routes
    return route


def only_route(world: World, settings: PlanSettings | None = None) -> Route:
    return only_route_of(world.snapshot(), settings)


def address_prefixes(route: Route) -> set[Prefix]:
    return {s.prefix for p in route.parts for a in p.addresses for s in a.sources}


def test_default_prefixes() -> None:
    w = World()
    w.site("example.com", "1.2.3.4", "2001:db8:1:2::5")
    assert cidrs(w.plan()) == ["1.2.2.0/23", "2001:db8:1::/48"]
    assert cidrs(w.plan(PlanSettings(default_prefix_v4=24, default_prefix_v6=64))) == ["1.2.3.0/24", "2001:db8:1:2::/64"]


def test_domain_prefix_overrides_groups() -> None:
    w = World()
    g = w.group(prefix_v4=20)
    w.site("example.com", "1.2.3.4", groups=(g,), prefix_v4=28)
    route = only_route(w)
    assert str(route.network) == "1.2.3.0/28"
    assert address_prefixes(route) == {Prefix(28, PrefixSource.DOMAIN)}


def test_group_prefix_overrides_default() -> None:
    w = World()
    g = w.group(prefix_v4=24)
    w.site("example.com", "1.2.3.4", groups=(g,))
    route = only_route(w)
    assert str(route.network) == "1.2.3.0/24"
    assert address_prefixes(route) == {Prefix(24, PrefixSource.GROUP, g)}


def test_default_prefix_source() -> None:
    w = World()
    w.site("example.com", "1.2.3.4")
    assert address_prefixes(only_route(w)) == {Prefix(23, PrefixSource.DEFAULT)}


def test_group_conflict_takes_widest() -> None:
    w = World()
    narrow, wide, plain = w.group(prefix_v4=28), w.group(prefix_v4=20), w.group()
    w.site("example.com", "1.2.3.4", groups=(narrow, wide, plain))
    route = only_route(w)
    assert str(route.network) == "1.2.0.0/20"
    assert address_prefixes(route) == {Prefix(20, PrefixSource.GROUP, wide)}


def test_group_prefix_tie_names_lowest_group() -> None:
    w = World()
    first, second = w.group(prefix_v4=24), w.group(prefix_v4=24)
    w.site("example.com", "1.2.3.4", groups=(second, first))
    assert address_prefixes(only_route(w)) == {Prefix(24, PrefixSource.GROUP, first)}


def test_disabled_group_prefix_ignored() -> None:
    w = World()
    off, on = w.group(enabled=False, prefix_v4=16), w.group()
    w.site("example.com", "1.2.3.4", groups=(off, on))
    assert cidrs(w.plan()) == ["1.2.2.0/23"]


def test_prefix_inherited_per_family() -> None:
    w = World()
    g = w.group(prefix_v4=24)
    w.site("example.com", "1.2.3.4", "2001:db8:1:2ff::5", groups=(g,), prefix_v6=56)
    plan = w.plan()
    assert cidrs(plan) == ["1.2.3.0/24", "2001:db8:1:200::/56"]
    assert [address_prefixes(r) for r in plan.routes] == [{Prefix(24, PrefixSource.GROUP, g)}, {Prefix(56, PrefixSource.DOMAIN)}]


@pytest.mark.parametrize(
    ("enabled", "groups", "routed"),
    [(True, (), True), (False, (), False), (True, (True,), True), (True, (False,), False), (True, (False, True), True), (True, (False, False), False), (False, (True,), False)],
)
def test_domain_routing_rule(enabled: bool, groups: tuple[bool, ...], routed: bool) -> None:
    w = World()
    w.site("example.com", "1.2.3.4", enabled=enabled, groups=tuple(w.group(enabled=g) for g in groups))
    assert cidrs(w.plan()) == (["1.2.2.0/23"] if routed else [])


@pytest.mark.parametrize(("enabled", "groups", "routed"), [(True, (), True), (False, (), False), (True, (True,), True), (True, (False,), False), (True, (False, True), True), (False, (True,), False)])
def test_global_route_routing_rule(enabled: bool, groups: tuple[bool, ...], routed: bool) -> None:
    w = World()
    w.global_route("149.154.160.0/20", enabled=enabled, groups=tuple(w.group(enabled=g) for g in groups))
    assert cidrs(w.plan()) == (["149.154.160.0/20"] if routed else [])


def test_addresses_of_all_names_use_domain_settings() -> None:
    w = World()
    w.domain("example.com", "www.example.com", prefix_v4=24)
    w.address("example.com", "1.2.3.4")
    w.address("www.example.com", "5.6.7.8")
    assert cidrs(w.plan()) == ["1.2.3.0/24", "5.6.7.0/24"]


def test_exclusions_list() -> None:
    settings = PlanSettings(server_addresses=(ip_address("203.0.113.10"), ip_address("2001:db8::1")), tunnel_networks=(ip_network("10.8.0.0/24"),), exclude=(ip_network("198.51.100.0/24"),))
    extra = exclusions(settings)[len(BUILTIN_EXCLUSIONS) :]
    assert [(str(e.network), e.reason) for e in extra] == [
        ("203.0.113.10/32", ExclusionReason.SERVER_ADDRESS),
        ("2001:db8::1/128", ExclusionReason.SERVER_ADDRESS),
        ("10.8.0.0/24", ExclusionReason.TUNNEL_NETWORK),
        ("198.51.100.0/24", ExclusionReason.SETTING),
    ]
    builtin = {e.network for e in BUILTIN_EXCLUSIONS}
    assert builtin == {
        ip_network(n)
        for n in (
            "0.0.0.0/8",
            "10.0.0.0/8",
            "100.64.0.0/10",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "224.0.0.0/3",
            "::/128",
            "::1/128",
            "::ffff:0:0/96",
            "fc00::/7",
            "fe80::/10",
            "ff00::/8",
        )
    }


@pytest.mark.parametrize("ip", ["10.1.2.3", "192.168.1.1", "127.0.0.1", "172.20.0.1", "100.64.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.1", "fd00::1", "fe80::1", "ff02::1"])
def test_builtin_exclusion_drops_network(ip: str) -> None:
    w = World()
    w.site("example.com", ip)
    plan = w.plan()
    assert plan.routes == ()
    [warning] = plan.warnings
    assert warning.kind == WarningKind.EXCLUDED
    assert warning.exclusion.reason == ExclusionReason.BUILTIN
    assert str(warning.network) in warning.message


@pytest.mark.parametrize("ip", ["::1", "::", "::ffff:1.2.3.4", "0.0.0.0", "127.0.0.1"])
def test_answer_inside_builtin_exclusion_is_no_address(ip: str) -> None:
    w = World()
    w.site("example.com", ip, "1.2.3.4", "2001:db8:1::5")
    plan = w.plan()
    assert cidrs(plan) == ["1.2.2.0/23", "2001:db8:1::/48"]
    assert (plan.stats.networks_v4, plan.stats.networks_v6) == (1, 1)
    assert [a.ip for r in plan.routes for p in r.parts for a in p.addresses] == [ip_address("1.2.3.4"), ip_address("2001:db8:1::5")]
    [warning] = plan.warnings
    assert (warning.kind, warning.network, warning.exclusion.reason) == (WarningKind.EXCLUDED, ip_network(ip), ExclusionReason.BUILTIN)


def test_answer_equal_to_server_address_still_trims() -> None:
    w = World()
    w.site("example.com", "1.2.3.4")
    plan = w.plan(PlanSettings(server_addresses=(ip_address("1.2.3.4"),)))
    assert cidrs(plan) == [str(n) for n in sorted(IPv4Network("1.2.2.0/23").address_exclude(IPv4Network("1.2.3.4/32")))]


def test_server_address_trims_network() -> None:
    w = World()
    w.site("example.com", "1.2.3.4")
    plan = w.plan(PlanSettings(server_addresses=(ip_address("1.2.3.10"),)))
    expected = IPv4Network("1.2.2.0/23").address_exclude(IPv4Network("1.2.3.10/32"))
    assert cidrs(plan) == [str(n) for n in sorted(expected)]
    assert all(ip_address("1.2.3.10") not in r.network for r in plan.routes)
    [warning] = plan.warnings
    assert (warning.kind, str(warning.network), str(warning.exclusion.network), warning.exclusion.reason) == (WarningKind.TRIMMED, "1.2.2.0/23", "1.2.3.10/32", ExclusionReason.SERVER_ADDRESS)
    assert "1.2.3.10/32" in warning.message


def test_trimmed_parts_keep_addresses_of_their_network() -> None:
    w = World()
    w.site("example.com", "1.2.3.4")
    plan = w.plan(PlanSettings(server_addresses=(ip_address("1.2.2.1"),)))
    assert all([a.ip for p in r.parts for a in p.addresses] == [ip_address("1.2.3.4")] for r in plan.routes)
    assert all(r.kind == RouteKind.DNS for r in plan.routes)


def test_tunnel_and_setting_exclusions_trim() -> None:
    w = World()
    w.site("a.example", "1.2.3.4")
    w.site("b.example", "5.6.7.8")
    plan = w.plan(PlanSettings(tunnel_networks=(ip_network("1.2.2.0/24"),), exclude=(ip_network("5.6.6.0/23"),)))
    assert cidrs(plan) == ["1.2.3.0/24"]
    assert {(x.kind, x.exclusion.reason) for x in plan.warnings} == {(WarningKind.TRIMMED, ExclusionReason.TUNNEL_NETWORK), (WarningKind.EXCLUDED, ExclusionReason.SETTING)}


def test_ipv6_server_address_trims() -> None:
    w = World()
    w.site("example.com", "2001:db8:1::5")
    plan = w.plan(PlanSettings(server_addresses=(ip_address("2001:db8:1::1"),)))
    assert len(plan.routes) == 128 - 48
    assert all(ip_address("2001:db8:1::1") not in r.network for r in plan.routes)


def test_global_routes_bypass_exclusions_and_aggregation() -> None:
    w = World()
    private = w.global_route("10.0.0.0/8")
    w.global_route("1.2.0.0/24")
    w.global_route("1.2.1.0/24")
    plan = w.plan(PlanSettings(exclude=(ip_network("1.2.0.0/16"),)))
    assert cidrs(plan) == ["1.2.0.0/24", "1.2.1.0/24", "10.0.0.0/8"]
    assert plan.warnings == ()
    route = plan.routes[2]
    assert (route.kind, route.global_route_id, route.parts) == (RouteKind.GLOBAL, private, ())


def test_dns_network_inside_global_route_is_dropped() -> None:
    w = World()
    gid = w.global_route("1.2.0.0/16")
    w.site("inside.example", "1.2.3.4")
    w.site("same.example", "5.6.7.8", prefix_v4=24)
    w.global_route("5.6.7.0/24")
    w.site("wider.example", "9.9.9.9", prefix_v4=16)
    w.global_route("9.9.9.0/24")
    plan = w.plan()
    assert cidrs(plan) == ["1.2.0.0/16", "5.6.7.0/24", "9.9.0.0/16", "9.9.9.0/24"]
    assert [r.kind for r in plan.routes] == [RouteKind.GLOBAL, RouteKind.GLOBAL, RouteKind.DNS, RouteKind.GLOBAL]
    assert plan.routes[0].global_route_id == gid


def test_retention_boundary() -> None:
    w = World()
    w.domain("example.com")
    w.address("example.com", "1.2.3.4", last_seen_at=NOW - 4 * DAY + 1)
    w.address("example.com", "5.6.7.8", last_seen_at=NOW - 4 * DAY)
    w.address("example.com", "9.9.9.9", last_seen_at=NOW - 30 * DAY, answered_at=NOW - 30 * DAY)
    assert cidrs(w.plan()) == ["1.2.2.0/23", "9.9.8.0/23"]
    assert cidrs(w.plan(PlanSettings(retention=Seconds(7 * DAY)))) == ["1.2.2.0/23", "5.6.6.0/23", "9.9.8.0/23"]


def test_only_checked_and_not_revoked_resolvers() -> None:
    w = World()
    unchecked, revoked, agent = w.resolver(use_for_routes=False), w.resolver(revoked=True), w.resolver()
    w.domain("example.com")
    w.address("example.com", "1.2.3.4", resolver=unchecked)
    w.address("example.com", "5.6.7.8", resolver=revoked)
    both = w.address("example.com", "9.9.9.9", resolver=unchecked)
    w.sighting(both, agent)
    w.sighting(both, SYSTEM, last_seen_at=NOW - 10 * DAY)
    route = only_route(w)
    assert str(route.network) == "9.9.8.0/23"
    [address] = route.parts[0].addresses
    assert address.resolvers == (agent,)


@pytest.mark.parametrize(
    ("push_ipv4", "push_ipv6", "expected"),
    [
        (True, True, ["1.2.2.0/23", "9.9.9.0/24", "2001:db8:1::/48", "2001:db8:ff::/48"]),
        (True, False, ["1.2.2.0/23", "9.9.9.0/24"]),
        (False, True, ["2001:db8:1::/48", "2001:db8:ff::/48"]),
        (False, False, []),
    ],
)
def test_family_flags(push_ipv4: bool, push_ipv6: bool, expected: list[str]) -> None:
    w = World()
    w.site("example.com", "1.2.3.4", "2001:db8:1::5")
    w.global_route("9.9.9.0/24")
    w.global_route("2001:db8:ff::/48")
    plan = w.plan(PlanSettings(push_ipv4=push_ipv4, push_ipv6=push_ipv6))
    assert cidrs(plan) == expected


def test_adjacent_networks_are_collapsed() -> None:
    w = World()
    w.site("a.example", "1.2.0.4")
    w.site("b.example", "1.2.2.4")
    w.site("c.example", "1.2.8.4")
    plan = w.plan()
    assert cidrs(plan) == ["1.2.0.0/22", "1.2.8.0/23"]
    aggregated, single = plan.routes
    assert aggregated.kind == RouteKind.AGGREGATED
    assert [str(p.network) for p in aggregated.parts] == ["1.2.0.0/23", "1.2.2.0/23"]
    assert single.kind == RouteKind.DNS
    assert [str(p.network) for p in single.parts] == ["1.2.8.0/23"]


def test_nested_network_is_absorbed_and_kept_as_part() -> None:
    w = World()
    w.site("wide.example", "1.2.3.4")
    w.site("narrow.example", "1.2.3.200", prefix_v4=24)
    route = only_route(w)
    assert (str(route.network), route.kind) == ("1.2.2.0/23", RouteKind.AGGREGATED)
    assert [str(p.network) for p in route.parts] == ["1.2.2.0/23", "1.2.3.0/24"]


def test_address_of_several_domains_lists_all_names() -> None:
    w = World()
    cloudflare = w.resolver()
    w.domain("a.example", "www.a.example")
    w.domain("b.example")
    w.address("a.example", "1.2.3.4")
    w.address("www.a.example", "1.2.3.4", resolver=cloudflare)
    w.address("b.example", "1.2.3.4")
    route = only_route(w)
    [part] = route.parts
    [address] = part.addresses
    assert address.ip == ip_address("1.2.3.4")
    assert address.names == tuple(w.names[n].id for n in ("a.example", "www.a.example", "b.example"))
    assert address.resolvers == (SYSTEM, cloudflare)


def test_removing_one_of_sharing_domains_keeps_route() -> None:
    w = World()
    first = w.site("a.example", "1.2.3.4")
    w.site("b.example", "1.2.3.4")
    snapshot = w.snapshot()
    names = tuple(n for n in snapshot.names if n.domain_id != first)
    addresses = tuple(a for a in snapshot.addresses if a.name_id in {n.id for n in names})
    sightings = tuple(s for s in snapshot.sightings if s.address_id in {a.id for a in addresses})
    route = only_route_of(replace(snapshot, domains=snapshot.domains[1:], names=names, addresses=addresses, sightings=sightings))
    assert str(route.network) == "1.2.2.0/23"
    assert route.parts[0].addresses[0].names == (w.names["b.example"].id,)


def test_aggregated_route_has_all_parts_with_their_addresses() -> None:
    w = World()
    g = w.group(prefix_v4=24)
    w.site("a.example", "1.2.0.1", "1.2.0.2", groups=(g,))
    w.site("b.example", "1.2.1.1", groups=(g,))
    route = only_route(w)
    assert (str(route.network), route.kind) == ("1.2.0.0/23", RouteKind.AGGREGATED)
    assert [(str(p.network), [str(a.ip) for a in p.addresses]) for p in route.parts] == [("1.2.0.0/24", ["1.2.0.1", "1.2.0.2"]), ("1.2.1.0/24", ["1.2.1.1"])]


def test_stats() -> None:
    w = World()
    w.site("a.example", "1.2.0.4", "1.2.2.4", "1.2.8.4", "2001:db8:1::5")
    w.global_route("9.9.9.0/24")
    w.global_route("2001:db8:ff::/48", enabled=False)
    stats = w.plan().stats
    assert (stats.networks_v4, stats.routes_v4, stats.networks_v6, stats.routes_v6) == (3, 3, 1, 1)


def test_plan_does_not_depend_on_snapshot_order() -> None:
    w = World()
    g = w.group(prefix_v4=22)
    w.site("a.example", "1.2.0.4", "2001:db8:1::5", groups=(g,))
    w.site("b.example", "1.2.4.4", "1.2.3.4")
    w.site("c.example", "1.2.3.4", "5.6.7.8")
    w.global_route("9.9.9.0/24")
    s = w.snapshot()
    reversed_snapshot = Snapshot(s.domains[::-1], s.groups[::-1], s.names[::-1], s.resolvers[::-1], s.addresses[::-1], s.sightings[::-1], s.global_routes[::-1])
    assert plan_routes(reversed_snapshot, PlanSettings()) == plan_routes(s, PlanSettings())
