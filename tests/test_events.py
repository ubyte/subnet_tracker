import re
import sqlite3

import pytest

from db_rows import insert
from domains_world import ACTOR, NOW, count, paste, paste_and_fail
from subnet_tracker.core.db import transaction
from subnet_tracker.core.domains import delete_domains, find_domain
from subnet_tracker.core.events import Event, EventFilter, EventKind, JsonValue, NewEvent, history_cutoff, list_events, prune_events, record_events
from subnet_tracker.types import DomainId, DomainName, EventId, ResolverId, Seconds, Timestamp

CLOUDFLARE: dict[str, JsonValue] = {"resolver_id": 2, "resolver": "Cloudflare"}


def events(db: sqlite3.Connection) -> tuple[Event, ...]:
    return list_events(db, EventFilter(100))


def test_kinds_are_dotted_and_unique() -> None:
    assert all(re.fullmatch(r"[a-z]+\.[a-z]+", kind) for kind in EventKind)
    assert len({k.value for k in EventKind}) == len(EventKind)


def test_kinds_named_by_the_design() -> None:
    named = {"domain.added", "domain.deleted", "address.new", "address.expired", "agent.created", "agent.revoked", "agent.rejected", "reconnect.done", "setting.changed"}
    assert named <= {k.value for k in EventKind}


def test_recorded_events_come_back_newest_first(db: sqlite3.Connection) -> None:
    first = NewEvent(EventKind.DOMAIN_ADDED, ACTOR, DomainId(7), DomainName("example.com"))
    second = NewEvent(EventKind.ADDRESS_NEW, "tick", DomainId(7), DomainName("www.example.com"), {"ip": "192.0.2.1", **CLOUDFLARE})
    record_events(db, [first], NOW)
    record_events(db, [second], Timestamp(NOW + 1))
    assert events(db) == (
        Event(EventId(2), Timestamp(NOW + 1), EventKind.ADDRESS_NEW, "tick", DomainId(7), DomainName("www.example.com"), {"ip": "192.0.2.1", **CLOUDFLARE}),
        Event(EventId(1), NOW, EventKind.DOMAIN_ADDED, ACTOR, DomainId(7), DomainName("example.com"), {}),
    )


def test_event_without_domain(db: sqlite3.Connection) -> None:
    record_events(db, [NewEvent(EventKind.SETTING_CHANGED, "cli:root", details={"key": "routes.retention", "old": "4d", "new": "7d"})], NOW)
    (event,) = events(db)
    assert (event.domain_id, event.name, event.details) == (None, None, {"key": "routes.retention", "old": "4d", "new": "7d"})


def test_many_events_in_one_call(db: sqlite3.Connection) -> None:
    record_events(db, (NewEvent(EventKind.DOMAIN_ADDED, ACTOR, DomainId(i)) for i in range(1, 6)), NOW)
    assert [e.domain_id for e in events(db)] == [5, 4, 3, 2, 1]


def test_event_rolls_back_with_the_change(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        paste_and_fail(db, "example.com", lambda: record_events(db, [NewEvent(EventKind.DOMAIN_ADDED, ACTOR, DomainId(1), DomainName("example.com"))], NOW))
    assert (count(db, "domains"), count(db, "events")) == (0, 0)


def test_events_nest_in_the_outer_transaction(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    with transaction(db):
        record_events(db, [NewEvent(EventKind.DOMAIN_ADDED, ACTOR)], NOW)
        assert count(other, "events") == 0
    assert count(other, "events") == 1


def test_event_outlives_its_domain(db: sqlite3.Connection) -> None:
    paste(db, "example.com")
    domain = find_domain(db, DomainName("example.com"))
    assert domain is not None
    with transaction(db):
        delete_domains(db, [domain.id])
        record_events(db, [NewEvent(EventKind.DOMAIN_DELETED, ACTOR, domain.id, DomainName("example.com"))], NOW)
    (event,) = events(db)
    assert (event.kind, event.domain_id, event.name) == (EventKind.DOMAIN_DELETED, domain.id, "example.com")


def test_unknown_kind_reads_as_text(db: sqlite3.Connection) -> None:
    insert(db, "events", {"ts": NOW, "kind": "group.renamed", "actor": "cli:root"})
    (event,) = events(db)
    assert event.kind == "group.renamed"
    assert not isinstance(event.kind, EventKind)


def test_known_kind_reads_as_enum(db: sqlite3.Connection) -> None:
    insert(db, "events", {"ts": NOW, "kind": "reconnect.done", "actor": "tick"})
    (event,) = events(db)
    assert event.kind is EventKind.RECONNECT_DONE


@pytest.fixture
def journal(db: sqlite3.Connection) -> sqlite3.Connection:
    record_events(
        db,
        [
            NewEvent(EventKind.DOMAIN_ADDED, ACTOR, DomainId(1), DomainName("example.com")),
            NewEvent(EventKind.ADDRESS_NEW, "tick", DomainId(1), DomainName("example.com"), {"ip": "192.0.2.1", **CLOUDFLARE}),
            NewEvent(EventKind.ADDRESS_NEW, "tick", DomainId(2), DomainName("example.org"), {"ip": "192.0.2.2", "resolver_id": 1, "resolver": "system"}),
            NewEvent(EventKind.AGENT_REVOKED, "cli:root", details=CLOUDFLARE),
            NewEvent(EventKind.DOMAIN_ADDED, "cli:root", DomainId(2), DomainName("example.org")),
        ],
        NOW,
    )
    return db


@pytest.mark.parametrize(
    ("where", "ids"),
    [
        (EventFilter(100), [5, 4, 3, 2, 1]),
        (EventFilter(100, kind=EventKind.DOMAIN_ADDED), [5, 1]),
        (EventFilter(100, domain_id=DomainId(1)), [2, 1]),
        (EventFilter(100, resolver_id=ResolverId(2)), [4, 2]),
        (EventFilter(100, actor="cli:root"), [5, 4]),
        (EventFilter(100, kind=EventKind.ADDRESS_NEW, domain_id=DomainId(2)), [3]),
        (EventFilter(100, kind=EventKind.ADDRESS_NEW, resolver_id=ResolverId(2), actor="tick"), [2]),
        (EventFilter(100, kind=EventKind.RECONNECT_DONE), []),
        (EventFilter(1, kind=EventKind.DOMAIN_ADDED), [5]),
    ],
)
def test_filters(journal: sqlite3.Connection, where: EventFilter, ids: list[int]) -> None:
    assert [e.id for e in list_events(journal, where)] == ids


def test_domain_filter_uses_the_index(db: sqlite3.Connection) -> None:
    plan = " ".join(row["detail"] for row in db.execute("EXPLAIN QUERY PLAN SELECT * FROM events WHERE domain_id = ? ORDER BY id DESC LIMIT ?", (1, 10)))
    assert "USING INDEX events_domain" in plan
    assert "TEMP B-TREE" not in plan


def test_history_cutoff() -> None:
    assert history_cutoff(NOW, Seconds(90 * 86400)) == NOW - 90 * 86400


def test_prune_keeps_events_at_the_cutoff(db: sqlite3.Connection) -> None:
    for ts in (NOW - 2, NOW - 1, NOW, NOW + 1):
        record_events(db, [NewEvent(EventKind.DOMAIN_ADDED, ACTOR)], Timestamp(ts))
    assert prune_events(db, NOW) == 2
    assert [e.ts for e in events(db)] == [NOW + 1, NOW]
