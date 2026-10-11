import sqlite3

import pytest

from domains_world import ACTOR, NOW, count, paste
from groups_world import domain, join, make_group, route
from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.groups import GroupEdit, NewGroup, get_group
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.core.services.groups import add_group, change_group, lookup_group, remove_group
from subnet_tracker.types import FieldError, GroupId, ReconnectMode, Timestamp

LATER = Timestamp(NOW + 10)


def work(db: sqlite3.Connection) -> TickWork | None:
    claim = claim_requests(db)
    return None if claim is None else claim.work


def kinds(db: sqlite3.Connection) -> list[EventKind | str]:
    return [e.kind for e in reversed(list_events(db, EventFilter(10)))]


def test_add_group_records_the_event_and_does_not_request_apply(db: sqlite3.Connection) -> None:
    added = add_group(db, NewGroup(" Video ", " hosting ", 24), ACTOR, NOW)
    assert not isinstance(added, FieldError)
    group = added.value
    assert (group.name, group.description, group.prefix_v4, group.enabled) == ("Video", "hosting", 24, True)
    assert not db.in_transaction
    (event,) = list_events(db, EventFilter(1))
    assert (event.kind, event.actor, dict(event.details)) == (
        EventKind.GROUP_ADDED,
        ACTOR,
        {"group_id": group.id, "name": "Video", "description": "hosting", "prefix_v4": 24, "prefix_v6": None, "enabled": True},
    )
    assert work(db) is None


@pytest.mark.parametrize("name", ["none", " NONE "])
def test_reserved_group_name_writes_nothing(db: sqlite3.Connection, name: str) -> None:
    assert add_group(db, NewGroup(name), ACTOR, NOW) == FieldError("name", f"{name.strip()!r} is reserved for domains without a group")
    assert (count(db, "groups"), count(db, "events"), count(db, "requests")) == (0, 0, 0)


def test_duplicate_group_name_writes_nothing(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    assert add_group(db, NewGroup("Video"), ACTOR, LATER) == FieldError("name", "group 'Video' already exists")
    assert count(db, "events") == 0


def test_edit_of_a_routing_field_requests_apply_and_leaves_domain_time(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video")
    paste(db, "a.com")
    join(db, domain(db, "a.com").id, group)
    updated = change_group(db, group.id, GroupEdit(prefix_v4=24, enabled=False), ACTOR, LATER)
    assert not isinstance(updated, FieldError)
    assert updated.changes == {"prefix_v4": (None, 24), "enabled": (True, False)}
    stored = get_group(db, group.id)
    assert stored is not None
    assert stored.updated_at == LATER
    assert domain(db, "a.com").updated_at == NOW
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)
    assert kinds(db) == [EventKind.GROUP_CHANGED]


def test_edit_of_the_description_does_not_request_apply(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video")
    updated = change_group(db, group.id, GroupEdit(description=" hosting "), ACTOR, LATER)
    assert not isinstance(updated, FieldError)
    assert updated.changes == {"description": ("", "hosting")}
    assert work(db) is None
    assert count(db, "events") == 1


def test_edit_that_keeps_the_values_does_not_touch_the_row(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video", prefix_v4=24)
    updated = change_group(db, group.id, GroupEdit(name=" Video ", prefix_v4=24, enabled=True), ACTOR, LATER)
    assert not isinstance(updated, FieldError)
    assert updated.changes == {}
    assert get_group(db, group.id) == group
    assert (count(db, "events"), count(db, "requests")) == (0, 0)


def test_rename_to_a_taken_name_or_none_changes_nothing(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    films = make_group(db, "Films")
    assert change_group(db, films.id, GroupEdit(name="Video", enabled=False), ACTOR, LATER) == FieldError("name", "group 'Video' already exists")
    assert change_group(db, films.id, GroupEdit(name="None"), ACTOR, LATER) == FieldError("name", "'None' is reserved for domains without a group")
    assert get_group(db, films.id) == films
    assert count(db, "events") == 0


def test_unknown_group_is_a_field_error(db: sqlite3.Connection) -> None:
    assert lookup_group(db, "Missing") == FieldError("name", "'Missing' does not exist")
    assert change_group(db, GroupId(99), GroupEdit(enabled=False), ACTOR, LATER) == FieldError("name", "does not exist")


def test_delete_of_an_empty_group_does_not_request_apply(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video")
    removed = remove_group(db, group.id, False, ACTOR, LATER)
    assert not isinstance(removed, FieldError)
    assert (removed.domains, removed.routes) == ((), ())
    assert get_group(db, group.id) is None
    assert work(db) is None
    (event,) = list_events(db, EventFilter(1))
    assert dict(event.details) == {"group_id": group.id, "name": "Video", "domains": 0, "routes": 0}


def test_delete_keeps_members_and_requests_apply_when_the_group_was_used(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video", enabled=False)
    paste(db, "a.com")
    join(db, domain(db, "a.com").id, group)
    route(db, "192.0.2.0/24", group)
    removed = remove_group(db, group.id, False, ACTOR, LATER)
    assert not isinstance(removed, FieldError)
    assert (removed.domains, removed.routes) == ((), ())
    assert domain(db, "a.com").updated_at == NOW
    assert count(db, "global_routes") == 1
    assert kinds(db) == [EventKind.GROUP_DELETED]
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


def test_delete_with_members_removes_only_exclusive_ones_and_records_them(db: sqlite3.Connection) -> None:
    video, music = make_group(db, "Video"), make_group(db, "Music")
    paste(db, "only.com both.com")
    join(db, domain(db, "only.com").id, video)
    join(db, domain(db, "both.com").id, video, music)
    route(db, "192.0.2.0/24", video)
    route(db, "198.51.100.0/24", video, music)
    removed = remove_group(db, video.id, True, ACTOR, LATER)
    assert not isinstance(removed, FieldError)
    assert [d.canonical.name for d in removed.domains] == ["only.com"]
    assert [str(r.network) for r in removed.routes] == ["192.0.2.0/24"]
    assert domain(db, "both.com").updated_at == NOW
    assert sorted(cidr for (cidr,) in db.execute("SELECT cidr FROM global_routes")) == ["198.51.100.0/24"]
    assert kinds(db) == [EventKind.DOMAIN_DELETED, EventKind.ROUTE_DELETED, EventKind.GROUP_DELETED]
    (deleted,) = [e for e in list_events(db, EventFilter(5)) if e.kind is EventKind.GROUP_DELETED]
    assert dict(deleted.details) == {"group_id": video.id, "name": "Video", "domains": 1, "routes": 1}
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


def add_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        add_group(db, NewGroup("Video"), ACTOR, NOW)
        raise RuntimeError


def test_add_rolls_back_with_the_outer_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        add_and_fail(db)
    assert (count(db, "groups"), count(db, "events"), count(db, "requests")) == (0, 0, 0)
