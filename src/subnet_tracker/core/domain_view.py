import json
import sqlite3
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from subnet_tracker.core.db import snapshot
from subnet_tracker.core.domains import Domain, get_domains, list_domains
from subnet_tracker.core.name_status import AddressState, NameStatus, ResolveResult, Sighting, address_state, domain_status, name_status
from subnet_tracker.core.names import Name
from subnet_tracker.routes.model import Group
from subnet_tracker.routes.plan import routable
from subnet_tracker.types import DomainId, GroupId, NameId, ResolveStatus, Seconds

_IN_DOMAINS = "IN (SELECT value FROM json_each(?))"
_NAMES_OF_DOMAINS = f"SELECT id FROM names WHERE domain_id {_IN_DOMAINS}"
_GROUPS = f"SELECT dg.domain_id, g.id, g.name, g.enabled FROM domain_groups dg JOIN groups g ON g.id = dg.group_id WHERE dg.domain_id {_IN_DOMAINS} ORDER BY g.name"
_TAGS = f"SELECT dt.domain_id, t.name FROM domain_tags dt JOIN tags t ON t.id = dt.tag_id WHERE dt.domain_id {_IN_DOMAINS} ORDER BY t.name"
_RESULTS = (
    "SELECT nr.name_id, r.name AS resolver, r.use_for_routes AND a.revoked_at IS NULL AS routes, nr.status, nr.attempted_at, nr.answered_at, nr.succeeded_at, nr.error"
    f" FROM name_resolves nr JOIN resolvers r ON r.id = nr.resolver_id LEFT JOIN agents a ON a.resolver_id = r.id WHERE nr.name_id IN ({_NAMES_OF_DOMAINS}) ORDER BY r.position, r.id"
)
_SIGHTINGS = (
    "SELECT ad.name_id, ad.ip, r.name AS resolver, s.first_seen_at, s.last_seen_at, nr.answered_at FROM addresses ad JOIN sightings s ON s.address_id = ad.id"
    " JOIN resolvers r ON r.id = s.resolver_id LEFT JOIN name_resolves nr ON nr.name_id = ad.name_id AND nr.resolver_id = s.resolver_id"
    f" WHERE ad.name_id IN ({_NAMES_OF_DOMAINS}) ORDER BY ad.family, ad.ip_bin, r.position, r.id"
)


@dataclass(frozen=True, slots=True)
class AddressView:
    ip: str
    sightings: tuple[Sighting, ...]


@dataclass(frozen=True, slots=True)
class NameView:
    name: Name
    # in the order of the resolver list
    results: tuple[ResolveResult, ...] = ()
    addresses: tuple[AddressView, ...] = ()

    @property
    def status(self) -> NameStatus | None:
        return name_status(self.results)


@dataclass(frozen=True, slots=True)
class DomainView:
    domain: Domain
    groups: tuple[str, ...]
    tags: tuple[str, ...]
    routable: bool
    names: tuple[NameView, NameView]

    @property
    def status(self) -> NameStatus | None:
        return domain_status(n.status for n in self.names)

    def stale(self, retention: Seconds) -> bool:
        return any(address_state(a.sightings, retention) is AddressState.STALE for n in self.names for a in n.addresses)


def _result(row: sqlite3.Row) -> ResolveResult:
    return ResolveResult(row["resolver"], bool(row["routes"]), ResolveStatus(row["status"]), row["attempted_at"], row["answered_at"], row["succeeded_at"], row["error"])


def _addresses(rows: Iterable[sqlite3.Row]) -> defaultdict[NameId, dict[str, list[Sighting]]]:
    addresses: defaultdict[NameId, dict[str, list[Sighting]]] = defaultdict(dict)
    for row in rows:
        addresses[row["name_id"]].setdefault(row["ip"], []).append(Sighting(row["resolver"], row["first_seen_at"], row["last_seen_at"], row["answered_at"]))
    return addresses


def _view(domain: Domain, groups: Sequence[sqlite3.Row], tags: Sequence[str], results: dict[NameId, list[ResolveResult]], addresses: dict[NameId, dict[str, list[Sighting]]]) -> DomainView:
    def name_view(name: Name) -> NameView:
        return NameView(name, tuple(results.get(name.id, ())), tuple(AddressView(ip, tuple(s)) for ip, s in addresses.get(name.id, {}).items()))

    known = {GroupId(g["id"]): Group(GroupId(g["id"]), bool(g["enabled"])) for g in groups}
    return DomainView(domain, tuple(g["name"] for g in groups), tuple(tags), routable(domain.enabled, frozenset(known), known), (name_view(domain.canonical), name_view(domain.pair)))


# newest first; all domains when ids is None
def domain_views(conn: sqlite3.Connection, ids: Collection[DomainId] | None = None) -> tuple[DomainView, ...]:
    groups: defaultdict[int, list[sqlite3.Row]] = defaultdict(list)
    tags: defaultdict[int, list[str]] = defaultdict(list)
    results: defaultdict[NameId, list[ResolveResult]] = defaultdict(list)
    with snapshot(conn):
        domains = list_domains(conn) if ids is None else get_domains(conn, ids)
        scope = (json.dumps([d.id for d in domains]),)
        for row in conn.execute(_GROUPS, scope):
            groups[row["domain_id"]].append(row)
        for row in conn.execute(_TAGS, scope):
            tags[row["domain_id"]].append(row["name"])
        for row in conn.execute(_RESULTS, scope):
            results[row["name_id"]].append(_result(row))
        addresses = _addresses(conn.execute(_SIGHTINGS, scope))
    return tuple(_view(d, groups[d.id], tags[d.id], results, addresses) for d in domains)
