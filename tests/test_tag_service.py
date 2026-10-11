import sqlite3

import pytest

from domains_world import ACTOR, NOW, count, paste
from groups_world import domain
from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.memberships import SetChange
from subnet_tracker.core.requests import claim_requests
from subnet_tracker.core.services.tags import lookup_tag, remove_tag, rename_named_tag
from subnet_tracker.core.tags import Tag, edit_domain_tags, ensure_tags, find_tag, list_tags
from subnet_tracker.types import DomainId, FieldError, TagId, Timestamp

LATER = Timestamp(NOW + 10)


def tag(db: sqlite3.Connection, name: str) -> Tag:
    found = find_tag(db, name)
    assert found is not None
    return found


def tag_names(db: sqlite3.Connection, domain_id: DomainId) -> set[str]:
    return {name for (name,) in db.execute("SELECT t.name FROM domain_tags dt JOIN tags t ON t.id = dt.tag_id WHERE dt.domain_id = ?", (domain_id,))}


def assign(db: sqlite3.Connection, domain_name: str, *ids: TagId) -> DomainId:
    paste(db, domain_name)
    found = domain(db, domain_name)
    edit_domain_tags(db, [found.id], SetChange(add=frozenset(ids)), NOW)
    return found.id


def test_rename_records_the_event_and_does_not_request_apply(db: sqlite3.Connection) -> None:
    assert not isinstance(ensure_tags(db, ["video"]), FieldError)
    video = tag(db, "video").id
    renamed = rename_named_tag(db, " video ", " films ", ACTOR, LATER)
    assert not isinstance(renamed, FieldError)
    assert (renamed.tag.name, renamed.old, renamed.merged, renamed.changed) == ("films", "video", False, True)
    assert find_tag(db, "video") is None
    (event,) = list_events(db, EventFilter(1))
    assert (event.kind, dict(event.details)) == (EventKind.TAG_RENAMED, {"tag_id": video, "old": "video", "new": "films"})
    assert claim_requests(db) is None


def test_rename_to_the_same_name_changes_nothing(db: sqlite3.Connection) -> None:
    ensure_tags(db, ["video"])
    renamed = rename_named_tag(db, "video", " video ", ACTOR, LATER)
    assert not isinstance(renamed, FieldError)
    assert renamed.changed is False
    assert (count(db, "events"), count(db, "tags")) == (0, 1)


def test_rename_into_a_taken_name_merges_and_leaves_domain_time(db: sqlite3.Connection) -> None:
    assert not isinstance(ensure_tags(db, ["video", "films"]), FieldError)
    video, films = tag(db, "video").id, tag(db, "films").id
    a = assign(db, "a.com", video, films)
    b = assign(db, "b.com", films)
    renamed = rename_named_tag(db, "video", "films", ACTOR, LATER)
    assert not isinstance(renamed, FieldError)
    assert (renamed.merged, renamed.tag.id) == (True, films)
    assert (tag_names(db, a), tag_names(db, b)) == ({"films"}, {"films"})
    assert [row.tag.name for row in list_tags(db)] == ["films"]
    assert domain(db, "a.com").updated_at == NOW
    (event,) = list_events(db, EventFilter(1))
    assert (event.kind, dict(event.details)) == (EventKind.TAG_MERGED, {"tag_id": films, "old": "video", "new": "films"})
    assert claim_requests(db) is None


def test_invalid_or_unknown_name_writes_nothing(db: sqlite3.Connection) -> None:
    ensure_tags(db, ["video"])
    assert rename_named_tag(db, "video", "a\nb", ACTOR, LATER) == FieldError("name", "line breaks are not allowed")
    assert lookup_tag(db, "missing") == FieldError("name", "'missing' does not exist")
    assert find_tag(db, "video") is not None
    assert count(db, "events") == 0


def test_delete_removes_the_tag_without_touching_domain_time(db: sqlite3.Connection) -> None:
    assert not isinstance(ensure_tags(db, ["video"]), FieldError)
    video = tag(db, "video").id
    a = assign(db, "a.com", video)
    removed = remove_tag(db, video, ACTOR, LATER)
    assert not isinstance(removed, FieldError)
    assert removed.value.name == "video"
    assert tag_names(db, a) == set()
    assert domain(db, "a.com").updated_at == NOW
    (event,) = list_events(db, EventFilter(1))
    assert (event.kind, dict(event.details)) == (EventKind.TAG_DELETED, {"tag_id": video, "name": "video"})
    assert claim_requests(db) is None


def rename_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        rename_named_tag(db, "video", "films", ACTOR, LATER)
        raise RuntimeError


def test_rename_rolls_back_with_the_outer_transaction(db: sqlite3.Connection) -> None:
    ensure_tags(db, ["video"])
    with pytest.raises(RuntimeError):
        rename_and_fail(db)
    assert find_tag(db, "video") is not None
    assert count(db, "events") == 0
