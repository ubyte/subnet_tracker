import sqlite3

import pytest

from domains_world import NOW, count
from groups_world import domain, domain_ids
from subnet_tracker.core.memberships import SetChange
from subnet_tracker.core.tags import Tag, TagRow, delete_tag, edit_domain_tags, ensure_tags, find_tag, list_tags, rename_tag
from subnet_tracker.types import MAX_TAG_NAME, DomainId, FieldError, TagId, Timestamp

LATER = Timestamp(NOW + 10)
LENGTH = f"must be 1 to {MAX_TAG_NAME} characters long"


def tags(db: sqlite3.Connection, *names: str) -> frozenset[TagId]:
    ids = ensure_tags(db, names)
    assert isinstance(ids, frozenset)
    return ids


def tag(db: sqlite3.Connection, name: str) -> Tag:
    found = find_tag(db, name)
    assert found is not None
    return found


def tag_names(db: sqlite3.Connection, domain_id: DomainId) -> set[str]:
    return {name for (name,) in db.execute("SELECT t.name FROM domain_tags dt JOIN tags t ON t.id = dt.tag_id WHERE dt.domain_id = ?", (domain_id,))}


def test_ensure_tags_creates_missing_and_returns_all_ids(db: sqlite3.Connection) -> None:
    (video,) = tags(db, "video")
    ids = tags(db, " video ", "two words", "google", "google")
    assert ids == {video, tag(db, "two words").id, tag(db, "google").id}
    assert [row.tag.name for row in list_tags(db)] == ["google", "two words", "video"]
    assert not db.in_transaction


def test_ensure_no_tags(db: sqlite3.Connection) -> None:
    assert tags(db) == frozenset()


@pytest.mark.parametrize(("name", "error"), [("", LENGTH), ("  ", LENGTH), ("x" * (MAX_TAG_NAME + 1), LENGTH), ("a\x85b", "line breaks are not allowed")])
def test_invalid_tag_name_creates_no_tags(db: sqlite3.Connection, name: str, error: str) -> None:
    assert ensure_tags(db, ["fine", name]) == FieldError("name", error)
    assert count(db, "tags") == 0


def test_tag_name_of_max_length_is_accepted(db: sqlite3.Connection) -> None:
    tags(db, "x" * MAX_TAG_NAME)
    assert tag(db, "x" * MAX_TAG_NAME).name == "x" * MAX_TAG_NAME


def test_find_tag_by_exact_name(db: sqlite3.Connection) -> None:
    (video,) = tags(db, "video")
    assert find_tag(db, "video") == Tag(video, "video")
    assert find_tag(db, "Video") is None


def test_list_tags_by_name_with_domain_counts(db: sqlite3.Connection) -> None:
    video, google, unused = (next(iter(tags(db, name))) for name in ("video", "Google", "unused"))
    a, b = domain_ids(db, "a.com b.com")
    edit_domain_tags(db, [a, b], SetChange(add=frozenset({video})), NOW)
    edit_domain_tags(db, [a], SetChange(add=frozenset({google})), NOW)
    assert list_tags(db) == (TagRow(Tag(google, "Google"), 1), TagRow(Tag(unused, "unused"), 0), TagRow(Tag(video, "video"), 2))


def test_rename_tag_trims_the_new_name(db: sqlite3.Connection) -> None:
    (video,) = tags(db, "video")
    assert rename_tag(db, video, " films ") == Tag(video, "films")
    assert find_tag(db, "video") is None


def test_rename_to_own_name_changes_nothing(db: sqlite3.Connection) -> None:
    (video,) = tags(db, "video")
    assert rename_tag(db, video, "video") == Tag(video, "video")


def test_rename_to_taken_name_merges_tags(db: sqlite3.Connection) -> None:
    video, films = (next(iter(tags(db, name))) for name in ("video", "films"))
    a, b, c = domain_ids(db, "a.com b.com c.com")
    edit_domain_tags(db, [a, b], SetChange(add=frozenset({video})), NOW)
    edit_domain_tags(db, [b, c], SetChange(add=frozenset({films})), NOW)
    assert rename_tag(db, video, "films") == Tag(films, "films")
    assert list_tags(db) == (TagRow(Tag(films, "films"), 3),)
    assert [tag_names(db, d) for d in (a, b, c)] == [{"films"}] * 3
    assert not db.in_transaction


@pytest.mark.parametrize(("name", "error"), [("", LENGTH), ("a\nb", "line breaks are not allowed")])
def test_invalid_rename_is_rejected(db: sqlite3.Connection, name: str, error: str) -> None:
    (video,) = tags(db, "video")
    assert rename_tag(db, video, name) == FieldError("name", error)
    assert find_tag(db, "video") == Tag(video, "video")


def test_rename_unknown_tag_returns_none(db: sqlite3.Connection) -> None:
    tags(db, "films")
    assert rename_tag(db, TagId(99), "films") is None
    assert rename_tag(db, TagId(99), "new") is None
    assert count(db, "tags") == 1


def test_delete_tag_keeps_its_domains(db: sqlite3.Connection) -> None:
    video, google = (next(iter(tags(db, name))) for name in ("video", "google"))
    (a,) = domain_ids(db, "a.com")
    edit_domain_tags(db, [a], SetChange(add=frozenset({video, google})), NOW)
    assert delete_tag(db, video)
    assert (tag_names(db, a), count(db, "domains")) == ({"google"}, 1)
    assert not delete_tag(db, video)


def test_edit_domain_tags_adds_removes_and_replaces(db: sqlite3.Connection) -> None:
    video, google, news = (next(iter(tags(db, name))) for name in ("video", "google", "news"))
    a, b = domain_ids(db, "a.com b.com")
    assert edit_domain_tags(db, [a, b, DomainId(99)], SetChange(add=frozenset({video, google, TagId(99)})), NOW) == 2
    assert edit_domain_tags(db, [a], SetChange(remove=frozenset({google})), LATER) == 1
    assert (tag_names(db, a), tag_names(db, b)) == ({"video"}, {"video", "google"})
    assert (domain(db, "a.com").updated_at, domain(db, "b.com").updated_at) == (LATER, NOW)
    assert edit_domain_tags(db, [a, b], SetChange(replace=frozenset({news})), LATER) == 2
    assert (tag_names(db, a), tag_names(db, b)) == ({"news"}, {"news"})
