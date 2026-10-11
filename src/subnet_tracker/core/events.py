import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from subnet_tracker.core.db import transaction
from subnet_tracker.types import DomainId, DomainName, EventId, JsonValue, ResolverId, Seconds, Timestamp

_INSERT = "INSERT INTO events (ts, kind, actor, domain_id, name, details) VALUES (?, ?, ?, ?, ?, ?)"
_SELECT = "SELECT id, ts, kind, actor, domain_id, name, details FROM events"


class EventKind(StrEnum):
    DOMAIN_ADDED = "domain.added"
    DOMAIN_CHANGED = "domain.changed"
    DOMAIN_DELETED = "domain.deleted"
    ADDRESS_NEW = "address.new"
    ADDRESS_EXPIRED = "address.expired"
    AGENT_CREATED = "agent.created"
    AGENT_REVOKED = "agent.revoked"
    AGENT_REJECTED = "agent.rejected"
    RECONNECT_DONE = "reconnect.done"
    SETTING_CHANGED = "setting.changed"
    GROUP_ADDED = "group.added"
    GROUP_CHANGED = "group.changed"
    GROUP_DELETED = "group.deleted"
    TAG_RENAMED = "tag.renamed"
    TAG_MERGED = "tag.merged"
    TAG_DELETED = "tag.deleted"
    ROUTE_ADDED = "route.added"
    ROUTE_CHANGED = "route.changed"
    ROUTE_DELETED = "route.deleted"
    RESOLVER_ADDED = "resolver.added"
    RESOLVER_CHANGED = "resolver.changed"
    RESOLVER_DELETED = "resolver.deleted"


@dataclass(frozen=True, slots=True)
class NewEvent:
    kind: EventKind
    actor: str
    domain_id: DomainId | None = None
    name: DomainName | None = None
    # events about a resolver carry resolver_id and its name as resolver
    details: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Event:
    id: EventId
    ts: Timestamp
    # text when another version wrote a kind this one does not know
    kind: EventKind | str
    actor: str
    domain_id: DomainId | None
    name: DomainName | None
    details: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class EventFilter:
    limit: int
    kind: EventKind | None = None
    domain_id: DomainId | None = None
    resolver_id: ResolverId | None = None
    actor: str | None = None


def _where(f: EventFilter) -> tuple[str, tuple[object, ...]]:
    terms = (("kind = ?", f.kind), ("domain_id = ?", f.domain_id), ("json_extract(details, '$.resolver_id') = ?", f.resolver_id), ("actor = ?", f.actor))
    used = [(sql, value) for sql, value in terms if value is not None]
    return (" WHERE " + " AND ".join(sql for sql, _ in used) if used else ""), tuple(value for _, value in used)


def _event(row: sqlite3.Row) -> Event:
    kind = EventKind(row["kind"]) if row["kind"] in EventKind else row["kind"]
    return Event(EventId(row["id"]), Timestamp(row["ts"]), kind, row["actor"], row["domain_id"], row["name"], json.loads(row["details"]))


def record_events(conn: sqlite3.Connection, events: Iterable[NewEvent], now: Timestamp) -> None:
    rows = ((now, e.kind, e.actor, e.domain_id, e.name, json.dumps(dict(e.details), ensure_ascii=False)) for e in events)
    with transaction(conn):
        conn.executemany(_INSERT, rows)


def list_events(conn: sqlite3.Connection, f: EventFilter) -> tuple[Event, ...]:
    where, values = _where(f)
    return tuple(_event(row) for row in conn.execute(f"{_SELECT}{where} ORDER BY id DESC LIMIT ?", (*values, f.limit)))


def history_cutoff(now: Timestamp, retention: Seconds) -> Timestamp:
    return Timestamp(now - retention)


def prune_events(conn: sqlite3.Connection, cutoff: Timestamp) -> int:
    return conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
