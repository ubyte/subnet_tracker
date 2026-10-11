import sqlite3
from dataclasses import dataclass, replace

from subnet_tracker.core.db import transaction
from subnet_tracker.core.edits import KEEP, Keep, edit_columns, insert_row, only_linked, taken, update_row
from subnet_tracker.core.resolver_endpoints import resolver_endpoint
from subnet_tracker.types import MAX_RESOLVER_NAME, FieldError, ResolverId, ResolverKind, Timestamp, one_line_name

MAX_QPS = 10_000


@dataclass(frozen=True, slots=True)
class Resolver:
    id: ResolverId
    name: str
    kind: ResolverKind
    endpoint: str | None
    use_for_routes: bool
    max_qps: int
    position: int
    created_at: Timestamp
    updated_at: Timestamp


@dataclass(frozen=True, slots=True)
class NewResolver:
    name: str
    kind: ResolverKind
    endpoint: str | None = None
    use_for_routes: bool = True
    max_qps: int = 50


DEFAULT_RESOLVERS = (
    NewResolver("system", ResolverKind.SYSTEM),
    NewResolver("Cloudflare", ResolverKind.DNS, "udp://1.1.1.1"),
    NewResolver("Google", ResolverKind.DNS, "udp://8.8.8.8"),
    NewResolver("Cloudflare v6", ResolverKind.DNS, "udp://[2606:4700:4700::1111]"),
    NewResolver("Cloudflare DoH", ResolverKind.DOH, "https://cloudflare-dns.com/dns-query", use_for_routes=False),
    NewResolver("Browser (Cloudflare)", ResolverKind.BROWSER, "https://cloudflare-dns.com/dns-query"),
)


@dataclass(frozen=True, slots=True)
class ResolverEdit:
    name: str | Keep = KEEP
    # checked against the kind, which never changes
    endpoint: str | Keep | None = KEEP
    use_for_routes: bool | Keep = KEEP
    max_qps: int | Keep = KEEP


@dataclass(frozen=True, slots=True)
class DeletedResolver:
    addresses: int  # left without sightings


def checked_resolver_edit(edit: ResolverEdit, kind: ResolverKind) -> ResolverEdit | FieldError:
    name = edit.name if isinstance(edit.name, Keep) else one_line_name("name", edit.name, MAX_RESOLVER_NAME)
    if isinstance(name, FieldError):
        return name
    endpoint = edit.endpoint if isinstance(edit.endpoint, Keep) else resolver_endpoint(kind, edit.endpoint)
    if isinstance(endpoint, FieldError):
        return endpoint
    if not isinstance(edit.max_qps, Keep) and (isinstance(edit.max_qps, bool) or not 1 <= edit.max_qps <= MAX_QPS):
        return FieldError("max_qps", f"must be 1 to {MAX_QPS}")
    return replace(edit, name=name, endpoint=endpoint)


def _resolver(row: sqlite3.Row) -> Resolver:
    return Resolver(
        ResolverId(row["id"]),
        row["name"],
        ResolverKind(row["kind"]),
        row["endpoint"],
        bool(row["use_for_routes"]),
        row["max_qps"],
        row["position"],
        Timestamp(row["created_at"]),
        Timestamp(row["updated_at"]),
    )


def get_resolver(conn: sqlite3.Connection, resolver_id: ResolverId) -> Resolver | None:
    row = conn.execute("SELECT * FROM resolvers WHERE id = ?", (resolver_id,)).fetchone()
    return None if row is None else _resolver(row)


def find_resolver(conn: sqlite3.Connection, name: str) -> Resolver | None:
    row = conn.execute("SELECT * FROM resolvers WHERE name = ?", (name,)).fetchone()
    return None if row is None else _resolver(row)


def list_resolvers(conn: sqlite3.Connection) -> tuple[Resolver, ...]:
    return tuple(map(_resolver, conn.execute("SELECT * FROM resolvers ORDER BY position, id")))


def create_resolver(conn: sqlite3.Connection, resolver: NewResolver, now: Timestamp) -> Resolver | FieldError:
    if resolver.kind == ResolverKind.AGENT:
        return FieldError("kind", "agent resolvers are created with their agent")
    checked = checked_resolver_edit(ResolverEdit(resolver.name, resolver.endpoint, resolver.use_for_routes, resolver.max_qps), resolver.kind)
    if isinstance(checked, FieldError):
        return checked
    with transaction(conn):
        if error := taken(conn, "resolvers", "name", "resolver", checked.name, None):
            return error
        position = conn.execute("SELECT coalesce(max(position) + 1, 0) FROM resolvers").fetchone()[0]
        return _resolver(insert_row(conn, "resolvers", {**edit_columns(checked), "kind": resolver.kind, "position": position}, now))


def create_default_resolvers(conn: sqlite3.Connection, now: Timestamp) -> tuple[Resolver, ...]:
    with transaction(conn):
        created = [create_resolver(conn, resolver, now) for resolver in DEFAULT_RESOLVERS]
        if errors := [c for c in created if isinstance(c, FieldError)]:
            raise ValueError(f"invalid default resolvers: {errors}")
    return tuple(c for c in created if isinstance(c, Resolver))


def edit_resolver(conn: sqlite3.Connection, resolver_id: ResolverId, edit: ResolverEdit, now: Timestamp) -> Resolver | FieldError | None:
    with transaction(conn):
        if (resolver := get_resolver(conn, resolver_id)) is None:
            return None
        checked = checked_resolver_edit(edit, resolver.kind)
        if isinstance(checked, FieldError):
            return checked
        if error := taken(conn, "resolvers", "name", "resolver", checked.name, resolver_id):
            return error
        update_row(conn, "resolvers", resolver_id, edit_columns(checked), now)
        return get_resolver(conn, resolver_id)


def delete_resolver(conn: sqlite3.Connection, resolver_id: ResolverId) -> DeletedResolver | None:
    with transaction(conn):
        addresses = conn.execute(f"DELETE FROM addresses WHERE id IN ({only_linked('sightings', 'address_id', 'resolver_id')})", (resolver_id,)).rowcount
        found = conn.execute("DELETE FROM resolvers WHERE id = ?", (resolver_id,)).rowcount
    return DeletedResolver(addresses) if found else None
