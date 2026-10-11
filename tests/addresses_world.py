import sqlite3
from ipaddress import ip_address

from db_rows import insert
from domains_world import NOW, paste
from subnet_tracker.core.addresses import NameResolution, Resolution
from subnet_tracker.core.services.resolves import Recorded, record_resolutions
from subnet_tracker.types import DomainName, NameId, ResolverId, ResolverKind, ResolveStatus, Timestamp

DAY = 86400
_ENDPOINTS = {ResolverKind.DNS: "udp://1.1.1.1", ResolverKind.DOH: "https://cloudflare-dns.com/dns-query", ResolverKind.BROWSER: "https://cloudflare-dns.com/dns-query"}


def name_id(db: sqlite3.Connection, name: str) -> NameId:
    return NameId(db.execute("SELECT id FROM names WHERE name = ?", (name,)).fetchone()[0])


def tracked(db: sqlite3.Connection, text: str) -> tuple[NameId, ...]:
    paste(db, text)
    return tuple(name_id(db, name) for name in text.split())


def resolver(db: sqlite3.Connection, name: str, kind: ResolverKind = ResolverKind.DNS) -> ResolverId:
    position = db.execute("SELECT count(*) FROM resolvers").fetchone()[0]
    return ResolverId(insert(db, "resolvers", {"name": name, "kind": kind, "endpoint": _ENDPOINTS.get(kind), "position": position, "created_at": 0, "updated_at": 0}))


def ok(*ips: str, cnames: tuple[str, ...] = ()) -> Resolution:
    return Resolution(ResolveStatus.OK, frozenset(ip_address(ip) for ip in ips), tuple(DomainName(c) for c in cnames))


def answer(name: NameId, resolver_id: ResolverId, resolution: Resolution, reporter: str | None = None) -> NameResolution:
    return NameResolution(name, resolver_id, resolution, reporter)


def record(db: sqlite3.Connection, *results: NameResolution, at: int = NOW, actor: str = "tick") -> Recorded:
    return record_resolutions(db, results, actor, Timestamp(at))


def rows(db: sqlite3.Connection, sql: str, *args: object) -> list[tuple[object, ...]]:
    return [tuple(row) for row in db.execute(sql, args)]
