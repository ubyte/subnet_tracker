import sqlite3

import pytest

from domains_world import ACTOR, NOW, count, dn, paste
from groups_world import domain, group_names, join, make_group
from subnet_tracker.core.batches import AddResult, get_batch
from subnet_tracker.core.db import transaction
from subnet_tracker.core.domain_view import domain_views
from subnet_tracker.core.domains import DomainEdit, edit_domains
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.requests import TickWork, claim_requests, finish_requests
from subnet_tracker.core.services.batches import undo_add
from subnet_tracker.core.services.domains import AddOptions, DomainChange, add_domain_names, add_pasted, change_domains, remove_domains
from subnet_tracker.types import BatchId, BatchKind, DomainId, FieldError, JsonValue, ReconnectMode, Timestamp

LATER = Timestamp(NOW + 1)
AUTO = AddOptions(ReconnectMode.AUTO)


def work(db: sqlite3.Connection) -> TickWork | None:
    claim = claim_requests(db)
    return None if claim is None else claim.work


def recorded(db: sqlite3.Connection) -> list[tuple[EventKind | str, str, DomainId | None, str | None, JsonValue]]:
    return [(e.kind, e.actor, e.domain_id, e.name, dict(e.details)) for e in reversed(list_events(db, EventFilter(100)))]


def written(db: sqlite3.Connection) -> tuple[int, int, int, int, int]:
    return count(db, "add_batches"), count(db, "domains"), count(db, "tags"), count(db, "events"), count(db, "requests")


def tags_of(db: sqlite3.Connection, name: str) -> tuple[str, ...]:
    (view,) = domain_views(db, [domain(db, name).id])
    return view.tags


def test_paste_adds_domains_with_their_events_and_requests(db: sqlite3.Connection) -> None:
    result = add_pasted(db, BatchKind.PASTE, ACTOR, "a.com www.b.com junk", AddOptions(ReconnectMode.ON_LEAVE), NOW)
    assert isinstance(result, AddResult)
    assert not db.in_transaction
    a, b = domain(db, "a.com").id, domain(db, "b.com").id
    batch = {"batch_id": result.batch_id}
    assert recorded(db) == [(EventKind.DOMAIN_ADDED, ACTOR, a, "a.com", batch), (EventKind.DOMAIN_ADDED, ACTOR, b, "www.b.com", batch)]
    assert work(db) == TickWork(new_domains=frozenset({a, b}), apply=ReconnectMode.ON_LEAVE)


def test_only_duplicates_record_the_batch_without_events_or_requests(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    result = add_pasted(db, BatchKind.CLI, ACTOR, "www.a.com", AUTO, NOW)
    assert isinstance(result, AddResult)
    assert (result.added, result.duplicates) == ((), dn("www.a.com"))
    assert written(db) == (2, 1, 0, 0, 0)


def add_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        add_pasted(db, BatchKind.PASTE, ACTOR, "a.com", AUTO, NOW)
        raise RuntimeError


def test_add_rolls_back_with_the_outer_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        add_and_fail(db)
    assert written(db) == (0, 0, 0, 0, 0)


def test_added_names_get_the_settings_while_duplicates_keep_theirs(db: sqlite3.Connection) -> None:
    make_group(db, "Work")
    paste(db, "old.com")
    change = DomainChange(DomainEdit(prefix_v4=24, prefix_v6=40, comment=" static "), add_groups=("Work",), add_tags=("video", "art"))
    result = add_domain_names(db, BatchKind.CLI, ACTOR, "new.com\nwww.old.com", dn("new.com", "www.old.com"), AddOptions(ReconnectMode.AUTO, change=change), NOW)
    assert isinstance(result, AddResult)
    assert result.duplicates == dn("www.old.com")
    new, old = domain(db, "new.com"), domain(db, "old.com")
    assert (new.prefix_v4, new.prefix_v6, new.comment, new.source, new.batch_id) == (24, 40, "static", "manual", result.batch_id)
    assert (group_names(db, new.id), tags_of(db, "new.com")) == ({"Work"}, ("art", "video"))
    assert (old.prefix_v4, old.comment, group_names(db, old.id), tags_of(db, "old.com")) == (None, "", set(), ())
    assert work(db) == TickWork(new_domains=frozenset({new.id}), apply=ReconnectMode.AUTO)


def test_no_resolve_requests_only_apply(db: sqlite3.Connection) -> None:
    add_domain_names(db, BatchKind.CLI, ACTOR, "a.com", dn("a.com"), AddOptions(ReconnectMode.AUTO, resolve=False), NOW)
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (DomainChange(add_groups=("Play",)), FieldError("group", "'Play' does not exist")),
        (DomainChange(DomainEdit(prefix_v4=7), add_tags=("video",)), FieldError("prefix_v4", "prefix /7 is out of range, expected 8 to 32")),
        (DomainChange(DomainEdit(comment="a\nb")), FieldError("comment", "line breaks are not allowed")),
        (DomainChange(add_tags=("video", "x" * 51)), FieldError("tag", "must be 1 to 50 characters long")),
    ],
)
def test_rejected_settings_write_nothing(db: sqlite3.Connection, change: DomainChange, error: FieldError) -> None:
    assert add_domain_names(db, BatchKind.CLI, ACTOR, "a.com", dn("a.com"), AddOptions(ReconnectMode.AUTO, change=change), NOW) == error
    assert written(db) == (0, 0, 0, 0, 0)


def test_change_edits_only_domains_that_differ(db: sqlite3.Connection) -> None:
    paste(db, "a.com b.com")
    a, b = domain(db, "a.com").id, domain(db, "b.com").id
    edit_domains(db, [a], DomainEdit(prefix_v4=24), NOW)
    changed = change_domains(db, [a, b], DomainChange(DomainEdit(prefix_v4=24)), ACTOR, LATER)
    assert not isinstance(changed, FieldError)
    assert [(c.domain.id, c.details) for c in changed] == [(b, {"prefix_v4": {"old": None, "new": 24}})]
    assert (domain(db, "a.com").updated_at, domain(db, "b.com").updated_at) == (NOW, LATER)
    assert recorded(db) == [(EventKind.DOMAIN_CHANGED, ACTOR, b, "b.com", {"prefix_v4": {"old": None, "new": 24}})]
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


def test_change_records_old_and_new_values(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    a = domain(db, "a.com").id
    edit_domains(db, [a], DomainEdit(prefix_v4=24), NOW)
    join(db, a, make_group(db, "Work"))
    make_group(db, "Play")
    change = DomainChange(DomainEdit(prefix_v4=None, enabled=False, comment="x"), add_groups=("Play",), remove_groups=("Work",), add_tags=("video", "art"))
    changed = change_domains(db, [a], change, ACTOR, LATER)
    assert not isinstance(changed, FieldError)
    assert [c.details for c in changed] == [
        {
            "prefix_v4": {"old": 24, "new": None},
            "enabled": {"old": True, "new": False},
            "comment": {"old": "", "new": "x"},
            "groups": {"old": ["Work"], "new": ["Play"]},
            "tags": {"old": [], "new": ["art", "video"]},
        }
    ]


def test_unchanged_domains_write_nothing(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    assert change_domains(db, [domain(db, "a.com").id], DomainChange(DomainEdit(enabled=True), remove_tags=("video",)), ACTOR, LATER) == ()
    assert (count(db, "tags"), count(db, "events"), count(db, "requests"), domain(db, "a.com").updated_at) == (0, 0, 0, NOW)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (DomainChange(remove_groups=("Play",)), FieldError("group", "'Play' does not exist")),
        (DomainChange(DomainEdit(prefix_v6=129)), FieldError("prefix_v6", "prefix /129 is out of range, expected 16 to 128")),
    ],
)
def test_rejected_change_writes_nothing(db: sqlite3.Connection, change: DomainChange, error: FieldError) -> None:
    paste(db, "a.com")
    assert change_domains(db, [domain(db, "a.com").id], change, ACTOR, LATER) == error
    assert (count(db, "events"), count(db, "requests")) == (0, 0)


def test_remove_deletes_both_names_with_events_and_apply(db: sqlite3.Connection) -> None:
    paste(db, "www.a.com b.com")
    a = domain(db, "a.com").id
    removed = remove_domains(db, [a, DomainId(99)], ACTOR, LATER)
    assert [d.canonical.name for d in removed] == ["www.a.com"]
    assert [name for (name,) in db.execute("SELECT name FROM names ORDER BY id")] == ["b.com", "www.b.com"]
    assert recorded(db) == [(EventKind.DOMAIN_DELETED, ACTOR, a, "www.a.com", {})]
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


def test_remove_of_nothing_writes_nothing(db: sqlite3.Connection) -> None:
    assert remove_domains(db, [DomainId(99)], ACTOR, NOW) == ()
    assert (count(db, "events"), count(db, "requests")) == (0, 0)


def test_undo_deletes_the_remaining_domains_of_the_batch(db: sqlite3.Connection) -> None:
    batch = paste(db, "a.com b.com c.com").batch_id
    a, b, c = (domain(db, name).id for name in ("a.com", "b.com", "c.com"))
    remove_domains(db, [b], ACTOR, NOW)
    claim = claim_requests(db)
    assert claim is not None
    finish_requests(db, claim)
    removed = undo_add(db, batch, ACTOR, LATER)
    assert removed is not None
    assert [d.id for d in removed] == [a, c]
    assert recorded(db)[1:] == [(EventKind.DOMAIN_DELETED, ACTOR, a, "a.com", {"batch_id": batch}), (EventKind.DOMAIN_DELETED, ACTOR, c, "c.com", {"batch_id": batch})]
    summary = get_batch(db, batch)
    assert summary is not None
    assert (summary.batch.undone_at, summary.domains) == (LATER, ())
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


def test_undo_again_deletes_nothing_and_requests_nothing(db: sqlite3.Connection) -> None:
    batch = paste(db, "a.com").batch_id
    undo_add(db, batch, ACTOR, NOW)
    events = count(db, "events")
    assert undo_add(db, batch, ACTOR, LATER) == ()
    assert (count(db, "events"), count(db, "requests")) == (events, 1)


def test_undo_of_an_unknown_batch_is_none(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    assert undo_add(db, BatchId(99), ACTOR, NOW) is None
    assert count(db, "domains") == 1
