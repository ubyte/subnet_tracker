from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import IPv4Network

from subnet_tracker.routes.model import Route, RoutePlan, route_order
from subnet_tracker.types import Timestamp

HEADER = "# managed by subnet-tracker, do not edit"


@dataclass(frozen=True, slots=True)
class RenderedFile:
    text: str
    sha256: str


def push_line(route: Route) -> str:
    n = route.network
    return f'push "route {n.network_address} {n.netmask}"' if isinstance(n, IPv4Network) else f'push "route-ipv6 {n}"'


def routes_hash(text: str) -> str:
    return sha256("".join(f"{line}\n" for line in text.splitlines() if line.startswith("push ")).encode()).hexdigest()


def render_routes(plan: RoutePlan, generated_at: Timestamp) -> RenderedFile:
    routes = sorted(plan.routes, key=route_order)
    v4 = sum(r.network.version == 4 for r in routes)
    generated = datetime.fromtimestamp(generated_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = "".join(f"{line}\n" for line in (HEADER, f"# generated {generated}, {v4} IPv4 + {len(routes) - v4} IPv6 routes", *map(push_line, routes)))
    return RenderedFile(text, routes_hash(text))
