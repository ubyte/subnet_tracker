import sqlite3

import pytest

from db_rows import SHA256, insert
from domains_world import NOW, count, paste
from subnet_tracker.core.db import transaction
from subnet_tracker.core.edits import KEEP
from subnet_tracker.core.resolvers import (
    DeletedResolver,
    NewResolver,
    Resolver,
    ResolverEdit,
    checked_resolver_edit,
    create_resolver,
    delete_resolver,
    edit_resolver,
    find_resolver,
    get_resolver,
    list_resolvers,
)
from subnet_tracker.types import MAX_RESOLVER_NAME, FieldError, ResolverId, ResolverKind, Timestamp

LATER = Timestamp(NOW + 10)
CLOUDFLARE = NewResolver("Cloudflare", ResolverKind.DNS, "udp://1.1.1.1")


def make_resolver(db: sqlite3.Connection, resolver: NewResolver = CLOUDFLARE) -> Resolver:
    created = create_resolver(db, resolver, NOW)
    assert isinstance(created, Resolver)
    return created


def agent_resolver(db: sqlite3.Connection, name: str) -> ResolverId:
    return ResolverId(insert(db, "resolvers", {"name": name, "kind": "agent", "position": 9, "created_at": 0, "updated_at": 0}))


def test_create_resolver_stores_trimmed_values(db: sqlite3.Connection) -> None:
    resolver = make_resolver(db, NewResolver(" Cloudflare v6 ", ResolverKind.DNS, " udp://[2606:4700:4700:0::1111] ", False, 20))
    assert resolver == Resolver(resolver.id, "Cloudflare v6", ResolverKind.DNS, "udp://[2606:4700:4700::1111]", False, 20, 0, NOW, NOW)
    assert get_resolver(db, resolver.id) == resolver
    assert find_resolver(db, "Cloudflare v6") == resolver
    assert not db.in_transaction


def test_create_resolver_defaults_to_routes_and_50_qps(db: sqlite3.Connection) -> None:
    resolver = make_resolver(db, NewResolver("system", ResolverKind.SYSTEM))
    assert (resolver.endpoint, resolver.use_for_routes, resolver.max_qps) == (None, True, 50)


def test_new_resolver_goes_last(db: sqlite3.Connection) -> None:
    first = make_resolver(db, NewResolver("system", ResolverKind.SYSTEM))
    second = make_resolver(db)
    delete_resolver(db, first.id)
    third = make_resolver(db, NewResolver("DoH", ResolverKind.DOH, "https://cloudflare-dns.com/dns-query"))
    assert (first.position, second.position, third.position) == (0, 1, 2)


def test_unknown_resolver_is_not_found(db: sqlite3.Connection) -> None:
    make_resolver(db)
    assert find_resolver(db, "cloudflare") is None
    assert get_resolver(db, ResolverId(99)) is None


@pytest.mark.parametrize(
    ("resolver", "error"),
    [
        (NewResolver("", ResolverKind.SYSTEM), FieldError("name", f"must be 1 to {MAX_RESOLVER_NAME} characters long")),
        (NewResolver("x" * (MAX_RESOLVER_NAME + 1), ResolverKind.SYSTEM), FieldError("name", f"must be 1 to {MAX_RESOLVER_NAME} characters long")),
        (NewResolver("two\nlines", ResolverKind.SYSTEM), FieldError("name", "line breaks are not allowed")),
        (NewResolver("system", ResolverKind.SYSTEM, "udp://1.1.1.1"), FieldError("endpoint", "system resolvers have no endpoint")),
        (NewResolver("Cloudflare", ResolverKind.DNS), FieldError("endpoint", "dns resolvers need an endpoint")),
        (NewResolver("DoH", ResolverKind.DOH, "http://dns.google/resolve"), FieldError("endpoint", "invalid endpoint 'http://dns.google/resolve', expected an https:// URL")),
        (NewResolver("Cloudflare", ResolverKind.DNS, "udp://1.1.1.1", max_qps=0), FieldError("max_qps", "must be a positive integer")),
        (NewResolver("Cloudflare", ResolverKind.DNS, "udp://1.1.1.1", max_qps=-5), FieldError("max_qps", "must be a positive integer")),
        (NewResolver("Cloudflare", ResolverKind.DNS, "udp://1.1.1.1", max_qps=True), FieldError("max_qps", "must be a positive integer")),
        (NewResolver("laptop", ResolverKind.AGENT), FieldError("kind", "agent resolvers are created with their agent")),
    ],
)
def test_invalid_resolver_is_rejected(db: sqlite3.Connection, resolver: NewResolver, error: FieldError) -> None:
    assert create_resolver(db, resolver, NOW) == error
    assert count(db, "resolvers") == 0


def test_resolver_name_of_max_length_is_accepted(db: sqlite3.Connection) -> None:
    assert make_resolver(db, NewResolver("x" * MAX_RESOLVER_NAME, ResolverKind.SYSTEM)).name == "x" * MAX_RESOLVER_NAME


def test_duplicate_resolver_name_is_rejected_after_trimming(db: sqlite3.Connection) -> None:
    make_resolver(db)
    assert create_resolver(db, NewResolver(" Cloudflare ", ResolverKind.SYSTEM), NOW) == FieldError("name", "resolver 'Cloudflare' already exists")
    assert count(db, "resolvers") == 1


def test_list_resolvers_by_position(db: sqlite3.Connection) -> None:
    system = make_resolver(db, NewResolver("system", ResolverKind.SYSTEM))
    cloudflare = make_resolver(db)
    db.execute("UPDATE resolvers SET position = 5 WHERE id = ?", (system.id,))
    agent = agent_resolver(db, "laptop")
    assert [r.id for r in list_resolvers(db)] == [cloudflare.id, system.id, agent]


def test_checked_resolver_edit_trims_and_keeps_unmentioned_fields() -> None:
    edit = ResolverEdit(name=" a ", endpoint=" tls://[2606:4700:4700:0::1111]:853 ")
    assert checked_resolver_edit(edit, ResolverKind.DNS) == ResolverEdit(name="a", endpoint="tls://[2606:4700:4700::1111]:853", use_for_routes=KEEP, max_qps=KEEP)
    assert checked_resolver_edit(ResolverEdit(), ResolverKind.SYSTEM) == ResolverEdit()


def test_edit_resolver_changes_given_fields_and_updated_at(db: sqlite3.Connection) -> None:
    resolver = make_resolver(db)
    edited = edit_resolver(db, resolver.id, ResolverEdit(name=" CF ", endpoint="tls://1.1.1.1", use_for_routes=False, max_qps=10), LATER)
    assert edited == Resolver(resolver.id, "CF", ResolverKind.DNS, "tls://1.1.1.1", False, 10, 0, NOW, LATER)
    assert get_resolver(db, resolver.id) == edited


def test_empty_edit_returns_resolver_unchanged(db: sqlite3.Connection) -> None:
    resolver = make_resolver(db)
    assert edit_resolver(db, resolver.id, ResolverEdit(), LATER) == resolver


def test_edit_of_unknown_resolver_returns_none(db: sqlite3.Connection) -> None:
    assert edit_resolver(db, ResolverId(99), ResolverEdit(use_for_routes=False), LATER) is None


@pytest.mark.parametrize(
    ("kind", "endpoint", "edit", "error"),
    [
        (ResolverKind.SYSTEM, None, ResolverEdit(endpoint="udp://1.1.1.1"), FieldError("endpoint", "system resolvers have no endpoint")),
        (ResolverKind.DNS, "udp://1.1.1.1", ResolverEdit(endpoint=""), FieldError("endpoint", "dns resolvers need an endpoint")),
        (
            ResolverKind.DNS,
            "udp://1.1.1.1",
            ResolverEdit(endpoint="https://dns.google/resolve"),
            FieldError("endpoint", "invalid endpoint 'https://dns.google/resolve', expected udp://, tcp:// or tls:// with an IP address and an optional port"),
        ),
        (ResolverKind.DOH, "https://dns.google/resolve", ResolverEdit(endpoint=None), FieldError("endpoint", "doh resolvers need an endpoint")),
        (ResolverKind.DNS, "udp://1.1.1.1", ResolverEdit(name=" "), FieldError("name", f"must be 1 to {MAX_RESOLVER_NAME} characters long")),
        (ResolverKind.DNS, "udp://1.1.1.1", ResolverEdit(use_for_routes=False, max_qps=0), FieldError("max_qps", "must be a positive integer")),
    ],
)
def test_invalid_edit_is_checked_against_the_stored_kind_and_changes_nothing(db: sqlite3.Connection, kind: ResolverKind, endpoint: str | None, edit: ResolverEdit, error: FieldError) -> None:
    resolver = make_resolver(db, NewResolver("r", kind, endpoint))
    assert checked_resolver_edit(edit, kind) == error
    assert edit_resolver(db, resolver.id, edit, LATER) == error
    assert get_resolver(db, resolver.id) == resolver


def test_system_resolver_accepts_a_blank_endpoint(db: sqlite3.Connection) -> None:
    resolver = make_resolver(db, NewResolver("system", ResolverKind.SYSTEM))
    edited = edit_resolver(db, resolver.id, ResolverEdit(endpoint=" "), LATER)
    assert isinstance(edited, Resolver)
    assert edited.endpoint is None


def test_rename_to_taken_name_is_rejected_and_to_own_name_allowed(db: sqlite3.Connection) -> None:
    make_resolver(db, NewResolver("system", ResolverKind.SYSTEM))
    cloudflare = make_resolver(db)
    assert edit_resolver(db, cloudflare.id, ResolverEdit(name="system", max_qps=5), LATER) == FieldError("name", "resolver 'system' already exists")
    assert get_resolver(db, cloudflare.id) == cloudflare
    renamed = edit_resolver(db, cloudflare.id, ResolverEdit(name="Cloudflare "), LATER)
    assert isinstance(renamed, Resolver)
    assert renamed.name == "Cloudflare"


def test_agent_resolver_takes_the_routes_checkbox_and_a_new_name(db: sqlite3.Connection) -> None:
    agent = agent_resolver(db, "laptop")
    edited = edit_resolver(db, agent, ResolverEdit(name="laptop-alex", use_for_routes=False), LATER)
    assert isinstance(edited, Resolver)
    assert (edited.name, edited.kind, edited.endpoint, edited.use_for_routes) == ("laptop-alex", ResolverKind.AGENT, None, False)


def test_delete_resolver_cascades_and_drops_addresses_left_without_sightings(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    name_id = db.execute("SELECT id FROM names WHERE name = 'a.com'").fetchone()[0]
    gone, kept = make_resolver(db), make_resolver(db, NewResolver("system", ResolverKind.SYSTEM))
    only = insert(db, "addresses", {"name_id": name_id, "ip": "192.0.2.1", "family": 4, "ip_bin": bytes([192, 0, 2, 1])})
    shared = insert(db, "addresses", {"name_id": name_id, "ip": "192.0.2.2", "family": 4, "ip_bin": bytes([192, 0, 2, 2])})
    for address, resolver in ((only, gone.id), (shared, gone.id), (shared, kept.id)):
        insert(db, "sightings", {"address_id": address, "resolver_id": resolver, "first_seen_at": 0, "last_seen_at": 0})
    for resolver in (gone.id, kept.id):
        insert(db, "name_resolves", {"name_id": name_id, "resolver_id": resolver, "attempted_at": 0, "status": "ok"})
    insert(db, "resolve_leases", {"name_id": name_id, "resolver_id": gone.id, "leased_until": 0})
    insert(db, "agents", {"resolver_id": gone.id, "cert_sha256": SHA256, "cert_not_after": 0, "created_at": 0})
    run = insert(db, "resolve_runs", {"trigger": "agent", "resolver_id": gone.id, "started_at": 0})
    insert(db, "resolve_run_resolvers", {"run_id": run, "resolver_id": gone.id, "queries": 0, "ok": 0, "notfound": 0, "errors": 0, "unreachable": 0, "healthy": 1})
    assert delete_resolver(db, gone.id) == DeletedResolver(addresses=1)
    assert [r.id for r in list_resolvers(db)] == [kept.id]
    assert [ip for (ip,) in db.execute("SELECT ip FROM addresses")] == ["192.0.2.2"]
    assert [tuple(row) for row in db.execute("SELECT address_id, resolver_id FROM sightings")] == [(shared, kept.id)]
    assert [r for (r,) in db.execute("SELECT resolver_id FROM name_resolves")] == [kept.id]
    assert [count(db, t) for t in ("resolve_leases", "agents", "resolve_runs", "resolve_run_resolvers")] == [0, 0, 0, 0]
    assert not db.in_transaction


def test_delete_unknown_resolver_returns_none(db: sqlite3.Connection) -> None:
    make_resolver(db)
    assert delete_resolver(db, ResolverId(99)) is None
    assert count(db, "resolvers") == 1


def create_resolver_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        resolver = make_resolver(db)
        edit_resolver(db, resolver.id, ResolverEdit(max_qps=5), LATER)
        raise RuntimeError


def test_resolver_writes_compose_in_one_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        create_resolver_and_fail(db)
    assert count(db, "resolvers") == 0
