from collections.abc import Iterable
from ipaddress import IPv4Network, IPv6Network, ip_network

from subnet_tracker.routes.model import Exclusion, ExclusionReason, PlanSettings
from subnet_tracker.types import IPAddress

_BUILTIN = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3", "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8")
BUILTIN_EXCLUSIONS = tuple(Exclusion(ip_network(n), ExclusionReason.BUILTIN) for n in _BUILTIN)


def exclusions(settings: PlanSettings) -> tuple[Exclusion, ...]:
    return (
        *BUILTIN_EXCLUSIONS,
        *(Exclusion(ip_network(ip), ExclusionReason.SERVER_ADDRESS) for ip in settings.server_addresses),
        *(Exclusion(n, ExclusionReason.TUNNEL_NETWORK) for n in settings.tunnel_networks),
        *(Exclusion(n, ExclusionReason.SETTING) for n in settings.exclude),
    )


def builtin_of(ip: IPAddress) -> Exclusion | None:
    return next((e for e in BUILTIN_EXCLUSIONS if ip in e.network), None)


def family_holes[N: (IPv4Network, IPv6Network)](family: type[N], excluded: Iterable[Exclusion]) -> list[tuple[N, Exclusion]]:
    return [(n, e) for e in excluded if isinstance(n := e.network, family)]


def overlapping[N: (IPv4Network, IPv6Network)](network: N, holes: Iterable[tuple[N, Exclusion]]) -> list[tuple[N, Exclusion]]:
    return [(n, e) for n, e in holes if network.overlaps(n)]


def cut[N: (IPv4Network, IPv6Network)](piece: N, hole: N) -> Iterable[N]:
    if not piece.overlaps(hole):
        return (piece,)
    return () if piece.subnet_of(hole) else piece.address_exclude(hole)


def trim[N: (IPv4Network, IPv6Network)](network: N, holes: Iterable[N]) -> list[N]:
    pieces = [network]
    for hole in holes:
        pieces = [q for p in pieces for q in cut(p, hole)]
    return pieces
