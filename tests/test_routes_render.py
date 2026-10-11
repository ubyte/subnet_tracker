from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import IPv4Address, IPv6Address, ip_network
from random import Random

from hypothesis import given
from hypothesis import strategies as st

from routes_world import NOW, World
from subnet_tracker.config import IPNetwork
from subnet_tracker.routes.model import PlanStats, Route, RoutePlan
from subnet_tracker.routes.render import render_routes, routes_hash
from subnet_tracker.types import RouteKind, Timestamp

GENERATED = Timestamp(int(datetime(2026, 10, 11, 3, 20, tzinfo=UTC).timestamp()))
HEADER = "# managed by subnet-tracker, do not edit\n"


def plan_of(*networks: str) -> RoutePlan:
    routes = tuple(Route(ip_network(n), RouteKind.DNS) for n in networks)
    v4, v6 = sum(r.network.version == 4 for r in routes), sum(r.network.version == 6 for r in routes)
    return RoutePlan(routes, PlanStats(v4, v6, v4, v6), ())


def push_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith("#")]


def test_design_example() -> None:
    rendered = render_routes(plan_of("149.154.160.0/20", "172.217.16.0/23", "2001:67c:4e8::/48"), GENERATED)
    pushes = 'push "route 149.154.160.0 255.255.240.0"\npush "route 172.217.16.0 255.255.254.0"\npush "route-ipv6 2001:67c:4e8::/48"\n'
    assert rendered.text == HEADER + "# generated 2026-10-11T03:20:00Z, 2 IPv4 + 1 IPv6 routes\n" + pushes
    assert rendered.sha256 == sha256(pushes.encode()).hexdigest()


def test_ipv4_with_netmask_and_ipv6_as_short_cidr() -> None:
    rendered = render_routes(plan_of("1.2.3.4/32", "9.0.0.0/8", "2001:db8:0:0::1/128", "2001:db8:0:1::/64"), GENERATED)
    assert push_lines(rendered.text) == ['push "route 1.2.3.4 255.255.255.255"', 'push "route 9.0.0.0 255.0.0.0"', 'push "route-ipv6 2001:db8::1/128"', 'push "route-ipv6 2001:db8:0:1::/64"']


def test_lines_in_route_order_not_text_order() -> None:
    rendered = render_routes(plan_of("2001:db8::/32", "149.154.160.0/20", "9.0.0.0/8", "9.0.0.0/16", "64:ff9b::/96"), GENERATED)
    assert push_lines(rendered.text) == [
        'push "route 9.0.0.0 255.0.0.0"',
        'push "route 9.0.0.0 255.255.0.0"',
        'push "route 149.154.160.0 255.255.240.0"',
        'push "route-ipv6 64:ff9b::/96"',
        'push "route-ipv6 2001:db8::/32"',
    ]


def test_global_and_aggregated_routes_render_alike() -> None:
    w = World()
    w.site("a.example", "1.0.0.1", "1.0.1.1", prefix_v4=24)
    w.site("b.example", "2001:db8::1")
    w.global_route("8.8.8.0/24")
    plan = w.plan()
    assert {r.kind for r in plan.routes} == {RouteKind.AGGREGATED, RouteKind.DNS, RouteKind.GLOBAL}
    assert push_lines(render_routes(plan, NOW).text) == ['push "route 1.0.0.0 255.255.254.0"', 'push "route 8.8.8.0 255.255.255.0"', 'push "route-ipv6 2001:db8::/48"']


def test_hash_ignores_header_and_time() -> None:
    plan = plan_of("1.0.0.0/24", "2001:db8::/48")
    earlier, later = render_routes(plan, GENERATED), render_routes(plan, Timestamp(GENERATED + 3600))
    assert earlier.text != later.text
    assert earlier.sha256 == later.sha256 == routes_hash(earlier.text) == routes_hash(f"# edited by hand\n\n{later.text}# trailing comment\n")


def test_hash_follows_every_push_line() -> None:
    hashes = {render_routes(plan_of(*networks), GENERATED).sha256 for networks in [("1.0.0.0/24",), ("1.0.0.0/23",), ("1.0.0.0/24", "2.0.0.0/24"), ("2001:db8::/48",), ()]}
    assert len(hashes) == 5
    text = render_routes(plan_of("1.0.0.0/24"), GENERATED).text
    assert routes_hash(text.replace("1.0.0.0 ", "1.0.0.1 ")) != routes_hash(text)


def test_empty_plan() -> None:
    rendered = render_routes(plan_of(), GENERATED)
    assert rendered.text == HEADER + "# generated 2026-10-11T03:20:00Z, 0 IPv4 + 0 IPv6 routes\n"
    assert rendered.sha256 == sha256(b"").hexdigest() == routes_hash("")


v4_networks = st.builds(lambda a, p: ip_network((IPv4Address(a), p), strict=False), st.integers(0, 2**32 - 1), st.integers(0, 32))
v6_networks = st.builds(lambda a, p: ip_network((IPv6Address(a), p), strict=False), st.integers(0, 2**128 - 1), st.integers(0, 128))


def parsed(line: str) -> IPNetwork:
    match line.removeprefix("push ").strip('"').split():
        case ["route", address, mask]:
            return ip_network(f"{address}/{mask}")
        case ["route-ipv6", cidr]:
            return ip_network(cidr)
    raise AssertionError(line)


@given(st.lists(st.one_of(v4_networks, v6_networks), unique=True, max_size=20), st.randoms(use_true_random=False), st.integers(0, 2**31))
def test_rendering_depends_only_on_the_set_of_routes(networks: list[IPNetwork], random: Random, generated: int) -> None:
    plan = plan_of(*map(str, networks))
    shuffled = list(plan.routes)
    random.shuffle(shuffled)
    rendered = render_routes(plan, GENERATED)
    assert render_routes(RoutePlan(tuple(shuffled), plan.stats, ()), GENERATED) == rendered
    assert render_routes(plan, Timestamp(generated)).sha256 == rendered.sha256 == routes_hash(rendered.text)
    assert [parsed(line) for line in push_lines(rendered.text)] == sorted(networks, key=lambda n: (n.version, int(n.network_address), n.prefixlen))
