from collections.abc import Iterable
from datetime import UTC, datetime

from subnet_tracker.textio.lines import DISABLED
from subnet_tracker.textio.model import ExportAddress, ExportDomain, ExportFile, ExportGroup, ExportRoute
from subnet_tracker.textio.parse import FORMAT_VERSION
from subnet_tracker.textio.tokens import quote, quoted
from subnet_tracker.types import Timestamp


def _tail(groups: Iterable[str], enabled: bool, comment: str) -> list[str]:
    return [*(f"@{quote(g)}" for g in sorted(groups)), *([] if enabled else [DISABLED]), *([f"; {comment}"] if comment else [])]


def _domain(domain: ExportDomain, extra_groups: frozenset[str]) -> str:
    prefixes = [f"{family}/{prefix}" for family, prefix in (("", domain.prefix_v4), ("v6", domain.prefix_v6)) if prefix is not None]
    tags = [f"#{quote(t)}" for t in sorted(domain.tags)]
    return " ".join([domain.name, *prefixes, *tags, *_tail(extra_groups, domain.enabled, domain.comment)])


def _route(route: ExportRoute) -> str:
    return " ".join([str(route.network), *_tail(route.groups, route.enabled, route.comment)])


def _time(ts: Timestamp) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _address(address: ExportAddress) -> str:
    return f"{address.name} {address.ip} {quote(address.resolver)} {_time(address.first_seen)} {_time(address.last_seen)}"


def _group(group: ExportGroup, domains: Iterable[ExportDomain]) -> list[str]:
    values = {"description": group.description or None, "prefix_v4": group.prefix_v4, "prefix_v6": group.prefix_v6, "enabled": None if group.enabled else "false"}
    properties = [f"{key} = {value}" for key, value in values.items() if value is not None]
    lines = [_domain(d, d.groups - {group.name}) for d in domains]
    return [f"[group {quoted(group.name)}]", *properties, *([""] if properties and lines else []), *lines]


def _section(header: str, lines: Iterable[str]) -> list[str]:
    return [header, *rest] if (rest := list(lines)) else []


def render_export(model: ExportFile) -> str:
    sections: dict[str, list[ExportDomain]] = {}
    ungrouped: list[ExportDomain] = []
    for domain in sorted(model.domains, key=lambda d: d.name):
        # the schema has no primary group: the section is the first of the domain's groups by name
        if domain.groups:
            sections.setdefault(min(domain.groups), []).append(domain)
        else:
            ungrouped.append(domain)
    groups = {g.name: g for g in model.groups}
    blocks = [
        _section("[settings]", (f"{s.key} = {s.value}" for s in sorted(model.settings, key=lambda s: s.key))),
        *(_group(groups.get(name, ExportGroup(name)), sections.get(name, ())) for name in sorted(groups.keys() | sections.keys())),
        _section("[ungrouped]", (_domain(d, frozenset()) for d in ungrouped)),
        _section("[routes]", map(_route, sorted(model.routes, key=lambda r: (r.network.version, int(r.network.network_address), r.network.prefixlen)))),
    ]
    if model.addresses is not None:
        blocks.append(["[addresses]", *map(_address, sorted(model.addresses, key=lambda a: (a.name, a.ip.version, int(a.ip), a.resolver)))])
    return "\n\n".join([f"# subnet-tracker export v{FORMAT_VERSION}", *("\n".join(b) for b in blocks if b)]) + "\n"
