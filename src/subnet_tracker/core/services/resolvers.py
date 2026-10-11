import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from subnet_tracker.core.db import transaction
from subnet_tracker.core.edits import Keep
from subnet_tracker.core.events import EventKind, NewEvent
from subnet_tracker.core.resolvers import NewResolver, Resolver, ResolverEdit, checked_resolver_edit, create_resolver, delete_resolver, edit_resolver, find_resolver, get_resolver
from subnet_tracker.core.services.recording import Change, Recorded, details_of, record_change
from subnet_tracker.types import MAX_RESOLVER_NAME, FieldError, JsonValue, ResolverId, Timestamp, one_line_name

# only the routes checkbox changes which addresses the plan keeps
_ROUTING = frozenset({"use_for_routes"})


@dataclass(frozen=True, slots=True)
class ResolverUpdate:
    resolver: Resolver
    changes: Mapping[str, Change]
    applied: bool


@dataclass(frozen=True, slots=True)
class ResolverRemoval:
    resolver: Resolver
    addresses: int
    applied: bool


def lookup_resolver(conn: sqlite3.Connection, name: str) -> Resolver | FieldError:
    checked = one_line_name("name", name, MAX_RESOLVER_NAME)
    if isinstance(checked, FieldError):
        return checked
    found = find_resolver(conn, checked)
    return found if found is not None else FieldError("name", f"{checked!r} does not exist")


def _changes(resolver: Resolver, edit: ResolverEdit) -> dict[str, Change] | FieldError:
    checked = checked_resolver_edit(edit, resolver.kind)
    if isinstance(checked, FieldError):
        return checked
    changes: dict[str, Change] = {}
    if not isinstance(checked.name, Keep) and checked.name != resolver.name:
        changes["name"] = (resolver.name, checked.name)
    if not isinstance(checked.endpoint, Keep) and checked.endpoint != resolver.endpoint:
        changes["endpoint"] = (resolver.endpoint, checked.endpoint)
    if not isinstance(checked.use_for_routes, Keep) and checked.use_for_routes != resolver.use_for_routes:
        changes["use_for_routes"] = (resolver.use_for_routes, checked.use_for_routes)
    if not isinstance(checked.max_qps, Keep) and checked.max_qps != resolver.max_qps:
        changes["max_qps"] = (resolver.max_qps, checked.max_qps)
    return changes


def _added(resolver: Resolver) -> dict[str, JsonValue]:
    return {"resolver_id": resolver.id, "resolver": resolver.name, "kind": resolver.kind, "endpoint": resolver.endpoint, "use_for_routes": resolver.use_for_routes, "max_qps": resolver.max_qps}


def add_resolver(conn: sqlite3.Connection, resolver: NewResolver, actor: str, now: Timestamp) -> Recorded[Resolver] | FieldError:
    with transaction(conn):
        created = create_resolver(conn, resolver, now)
        if isinstance(created, FieldError):
            return created
        applied = record_change(conn, [NewEvent(EventKind.RESOLVER_ADDED, actor, details=_added(created))], now, apply=False)
    return Recorded(created, applied)


def change_resolver(conn: sqlite3.Connection, resolver_id: ResolverId, edit: ResolverEdit, actor: str, now: Timestamp) -> ResolverUpdate | FieldError:
    with transaction(conn):
        resolver = get_resolver(conn, resolver_id)
        if resolver is None:
            return FieldError("name", "does not exist")
        changes = _changes(resolver, edit)
        if isinstance(changes, FieldError) or not changes:
            return changes if isinstance(changes, FieldError) else ResolverUpdate(resolver, {}, False)
        edited = edit_resolver(conn, resolver_id, edit, now)
        if not isinstance(edited, Resolver):
            return edited if isinstance(edited, FieldError) else FieldError("name", "does not exist")
        applied = record_change(
            conn, [NewEvent(EventKind.RESOLVER_CHANGED, actor, details={"resolver_id": edited.id, "resolver": edited.name, **details_of(changes)})], now, apply=bool(_ROUTING.intersection(changes))
        )
    return ResolverUpdate(edited, changes, applied)


def remove_resolver(conn: sqlite3.Connection, resolver_id: ResolverId, actor: str, now: Timestamp) -> ResolverRemoval | FieldError:
    with transaction(conn):
        resolver = get_resolver(conn, resolver_id)
        if resolver is None:
            return FieldError("name", "does not exist")
        deleted = delete_resolver(conn, resolver_id)
        if deleted is None:
            return FieldError("name", "does not exist")
        applied = record_change(
            conn, [NewEvent(EventKind.RESOLVER_DELETED, actor, details={"resolver_id": resolver.id, "resolver": resolver.name, "addresses": deleted.addresses})], now, apply=resolver.use_for_routes
        )
    return ResolverRemoval(resolver, deleted.addresses, applied)
