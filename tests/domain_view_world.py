import sqlite3

from db_rows import insert
from subnet_tracker.types import ResolverId, ResolveStatus


def name_id(db: sqlite3.Connection, name: str) -> int:
    return int(db.execute("SELECT id FROM names WHERE name = ?", (name,)).fetchone()["id"])


def add_resolver(db: sqlite3.Connection, name: str, position: int, use_for_routes: bool = True) -> ResolverId:
    return ResolverId(
        insert(
            db, "resolvers", {"name": name, "kind": "dns", "endpoint": f"udp://192.0.2.{position + 1}", "use_for_routes": int(use_for_routes), "position": position, "created_at": 0, "updated_at": 0}
        )
    )


def resolved(db: sqlite3.Connection, name: str, resolver: ResolverId, status: ResolveStatus, at: int, error: str | None = None) -> None:
    answered = None if status in (ResolveStatus.ERROR, ResolveStatus.UNREACHABLE) else at
    row = {
        "name_id": name_id(db, name),
        "resolver_id": resolver,
        "attempted_at": at,
        "answered_at": answered,
        "succeeded_at": at if status is ResolveStatus.OK else None,
        "status": status,
        "error": error,
    }
    insert(db, "name_resolves", row)


def seen(db: sqlite3.Connection, name: str, ip: str, resolver: ResolverId, first: int, last: int) -> None:
    packed = bytes(int(part) for part in ip.split("."))
    address = db.execute("SELECT id FROM addresses WHERE name_id = ? AND ip = ?", (name_id(db, name), ip)).fetchone()
    address_id = address["id"] if address else insert(db, "addresses", {"name_id": name_id(db, name), "ip": ip, "family": 4, "ip_bin": packed})
    insert(db, "sightings", {"address_id": address_id, "resolver_id": resolver, "first_seen_at": first, "last_seen_at": last})


def resolve_times(db: sqlite3.Connection, name: str, attempt: int | None, success: int | None) -> None:
    db.execute("UPDATE names SET last_attempt_at = ?, last_success_at = ? WHERE name = ?", (attempt, success, name))
