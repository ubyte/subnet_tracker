import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from subnet_tracker.core.addresses import NameResolution, answer_times
from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import EventKind, NewEvent, record_events
from subnet_tracker.types import DomainId, DomainName, IPAddress, NameId, ResolverId, ResolveStatus, Timestamp

# A status that does not set answered_at or succeeded_at keeps the previous one. requested_at belongs to Resolve now.
_UPSERT = (
    "INSERT INTO name_resolves (name_id, resolver_id, attempted_at, answered_at, succeeded_at, status, error, cnames, reporter)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name_id, resolver_id) DO UPDATE SET"
    " attempted_at = excluded.attempted_at, answered_at = COALESCE(excluded.answered_at, name_resolves.answered_at),"
    " succeeded_at = COALESCE(excluded.succeeded_at, name_resolves.succeeded_at), status = excluded.status, error = excluded.error,"
    " cnames = excluded.cnames, reporter = excluded.reporter"
)
_TIMES = "UPDATE names SET last_attempt_at = ?, last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END WHERE id = ?"
_ADDRESS = "INSERT INTO addresses (name_id, ip, family, ip_bin) VALUES (?, ?, ?, ?) ON CONFLICT (name_id, ip) DO NOTHING RETURNING id"
_SIGHTING = "INSERT INTO sightings (address_id, resolver_id, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?) ON CONFLICT (address_id, resolver_id) DO UPDATE SET last_seen_at = excluded.last_seen_at"


@dataclass(frozen=True, slots=True)
class Recorded:
    addresses_new: int


def _names(conn: sqlite3.Connection, ids: Sequence[NameId]) -> dict[NameId, tuple[DomainId, DomainName]]:
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    rows = conn.execute(f"SELECT id, domain_id, name FROM names WHERE id IN ({marks})", tuple(ids))
    return {NameId(row["id"]): (DomainId(row["domain_id"]), DomainName(row["name"])) for row in rows}


def _resolvers(conn: sqlite3.Connection, ids: Sequence[ResolverId]) -> dict[ResolverId, str]:
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    rows = conn.execute(f"SELECT id, name FROM resolvers WHERE id IN ({marks})", tuple(ids))
    return {ResolverId(row["id"]): row["name"] for row in rows}


def _cnames(cnames: tuple[DomainName, ...]) -> str | None:
    return None if not cnames else json.dumps(list(cnames), ensure_ascii=False)


# Returns whether this call created the addresses row. An address missing from an ok answer is left as it was, so it goes stale.
def _see(conn: sqlite3.Connection, name_id: NameId, resolver_id: ResolverId, ip: IPAddress, now: Timestamp) -> bool:
    text = str(ip)
    row = conn.execute(_ADDRESS, (name_id, text, ip.version, ip.packed)).fetchone()
    fresh = row is not None
    if row is None:
        row = conn.execute("SELECT id FROM addresses WHERE name_id = ? AND ip = ?", (name_id, text)).fetchone()
    conn.execute(_SIGHTING, (row["id"], resolver_id, now, now))
    return fresh


def record_resolutions(conn: sqlite3.Connection, results: Sequence[NameResolution], actor: str, now: Timestamp) -> Recorded:
    if not results:
        return Recorded(0)
    with transaction(conn):
        # Gone while the resolve was in flight: skip the result instead of failing the rest of the batch.
        names = _names(conn, tuple({item.name_id for item in results}))
        resolvers = _resolvers(conn, tuple({item.resolver_id for item in results}))
        events: list[NewEvent] = []
        for item in results:
            place = names.get(item.name_id)
            resolver = resolvers.get(item.resolver_id)
            if place is None or resolver is None:
                continue
            resolution = item.resolution
            answered, succeeded = answer_times(resolution.status, now)
            conn.execute(_UPSERT, (item.name_id, item.resolver_id, now, answered, succeeded, resolution.status, resolution.error, _cnames(resolution.cnames), item.reporter))
            conn.execute(_TIMES, (now, int(resolution.status is ResolveStatus.OK), now, item.name_id))
            if resolution.status is not ResolveStatus.OK:
                continue
            domain_id, name = place
            for ip in sorted(resolution.addresses, key=lambda address: (address.version, address.packed)):
                if _see(conn, item.name_id, item.resolver_id, ip, now):
                    events.append(NewEvent(EventKind.ADDRESS_NEW, actor, domain_id, name, {"ip": str(ip), "resolver_id": item.resolver_id, "resolver": resolver}))
        if events:
            record_events(conn, events, now)
    return Recorded(len(events))
