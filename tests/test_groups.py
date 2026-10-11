import sqlite3

import pytest

from db_rows import write_locked
from domains_world import NOW, count, paste
from groups_world import domain, domain_ids, group_ids, group_names, join, make_group, route
from subnet_tracker.core.db import transaction
from subnet_tracker.core.domains import list_domains
from subnet_tracker.core.edits import KEEP
from subnet_tracker.core.groups import (
    DeletedGroup,
    Group,
    GroupEdit,
    GroupRow,
    NewGroup,
    checked_group_edit,
    create_group,
    delete_group,
    edit_domain_groups,
    edit_group,
    find_group,
    get_group,
    list_groups,
)
from subnet_tracker.core.memberships import SetChange
from subnet_tracker.types import MAX_GROUP_NAME, DomainId, FieldError, GroupId, Timestamp

LATER = Timestamp(NOW + 10)


def test_create_group_stores_trimmed_values(db: sqlite3.Connection) -> None:
    group = create_group(db, NewGroup("  Video ", " Video hosting ", 24, 40, False), NOW)
    assert isinstance(group, Group)
    assert group == Group(group.id, "Video", "Video hosting", 24, 40, False, NOW, NOW)
    assert get_group(db, group.id) == group
    assert find_group(db, "Video") == group
    assert not db.in_transaction


def test_create_group_defaults_to_enabled_with_inherited_prefixes(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video")
    assert (group.description, group.prefix_v4, group.prefix_v6, group.enabled) == ("", None, None, True)


def test_unknown_group_is_not_found(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    assert find_group(db, "video") is None
    assert get_group(db, GroupId(99)) is None


@pytest.mark.parametrize(
    ("group", "error"),
    [
        (NewGroup(""), FieldError("name", f"must be 1 to {MAX_GROUP_NAME} characters long")),
        (NewGroup(" \t "), FieldError("name", f"must be 1 to {MAX_GROUP_NAME} characters long")),
        (NewGroup("x" * (MAX_GROUP_NAME + 1)), FieldError("name", f"must be 1 to {MAX_GROUP_NAME} characters long")),
        (NewGroup("two\nlines"), FieldError("name", "line breaks are not allowed")),
        (NewGroup("none"), FieldError("name", "'none' is reserved for domains without a group")),
        (NewGroup(" NONE "), FieldError("name", "'NONE' is reserved for domains without a group")),
        (NewGroup("Video", "two\u2028lines"), FieldError("description", "line breaks are not allowed")),
        (NewGroup("Video", prefix_v4=7), FieldError("prefix_v4", "prefix /7 is out of range, expected 8 to 32")),
        (NewGroup("Video", prefix_v6=129), FieldError("prefix_v6", "prefix /129 is out of range, expected 16 to 128")),
    ],
)
def test_invalid_group_is_rejected(db: sqlite3.Connection, group: NewGroup, error: FieldError) -> None:
    assert create_group(db, group, NOW) == error
    assert count(db, "groups") == 0


def test_group_name_of_max_length_is_accepted(db: sqlite3.Connection) -> None:
    assert make_group(db, "x" * MAX_GROUP_NAME).name == "x" * MAX_GROUP_NAME


def test_duplicate_group_name_is_rejected_after_trimming(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    assert create_group(db, NewGroup(" Video "), NOW) == FieldError("name", "group 'Video' already exists")
    assert make_group(db, "video").name == "video"
    assert count(db, "groups") == 2


def test_edit_group_changes_given_fields_and_updated_at(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video", prefix_v4=24)
    edited = edit_group(db, group.id, GroupEdit(name=" Films ", description=" all video ", prefix_v6=40, enabled=False), LATER)
    assert edited == Group(group.id, "Films", "all video", 24, 40, False, NOW, LATER)
    assert get_group(db, group.id) == edited


def test_edit_group_sets_prefix_back_to_inherited(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video", prefix_v4=24, prefix_v6=40)
    edited = edit_group(db, group.id, GroupEdit(prefix_v4=None), LATER)
    assert isinstance(edited, Group)
    assert (edited.prefix_v4, edited.prefix_v6) == (None, 40)


def test_empty_edit_returns_group_unchanged(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video")
    assert edit_group(db, group.id, GroupEdit(), LATER) == group


def test_edit_of_unknown_group_returns_none(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    assert edit_group(db, GroupId(99), GroupEdit(enabled=False), LATER) is None
    assert edit_group(db, GroupId(99), GroupEdit(name="Video"), LATER) is None


def test_rename_to_own_name_is_allowed(db: sqlite3.Connection) -> None:
    group = make_group(db, "Video")
    edited = edit_group(db, group.id, GroupEdit(name="Video "), LATER)
    assert isinstance(edited, Group)
    assert edited.name == "Video"


def test_rename_to_taken_name_is_rejected(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    films = make_group(db, "Films")
    assert edit_group(db, films.id, GroupEdit(name="Video", enabled=False), LATER) == FieldError("name", "group 'Video' already exists")
    assert get_group(db, films.id) == films


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (GroupEdit(name=""), FieldError("name", f"must be 1 to {MAX_GROUP_NAME} characters long")),
        (GroupEdit(name="a\rb"), FieldError("name", "line breaks are not allowed")),
        (GroupEdit(name="None"), FieldError("name", "'None' is reserved for domains without a group")),
        (GroupEdit(description="a\nb"), FieldError("description", "line breaks are not allowed")),
        (GroupEdit(prefix_v4=33), FieldError("prefix_v4", "prefix /33 is out of range, expected 8 to 32")),
        (GroupEdit(prefix_v6=15), FieldError("prefix_v6", "prefix /15 is out of range, expected 16 to 128")),
        (GroupEdit(enabled=False, prefix_v4=True), FieldError("prefix_v4", "prefix must be an integer, got True")),
    ],
)
def test_invalid_edit_is_rejected_and_changes_nothing(db: sqlite3.Connection, edit: GroupEdit, error: FieldError) -> None:
    assert checked_group_edit(edit) == error
    group = make_group(db, "Video")
    assert edit_group(db, group.id, edit, LATER) == error
    assert get_group(db, group.id) == group


def test_checked_group_edit_trims_and_keeps_unmentioned_fields() -> None:
    assert checked_group_edit(GroupEdit(name=" a ", description=" b ")) == GroupEdit(name="a", description="b", prefix_v4=KEEP, prefix_v6=KEEP, enabled=KEEP)
    assert checked_group_edit(GroupEdit()) == GroupEdit()


def test_delete_group_keeps_its_domains_and_routes_without_the_group(db: sqlite3.Connection) -> None:
    video, music = make_group(db, "Video"), make_group(db, "Music")
    only, both = domain_ids(db, "only.com both.com")
    join(db, only, video)
    join(db, both, video, music)
    route_id = route(db, "192.0.2.0/24", video)
    assert delete_group(db, video.id) == DeletedGroup(0, 0)
    assert get_group(db, video.id) is None
    assert (group_names(db, only), group_names(db, both)) == (set(), {"Music"})
    assert count(db, "domains") == 2
    assert db.execute("SELECT count(*) FROM route_groups WHERE route_id = ?", (route_id,)).fetchone()[0] == 0
    assert count(db, "global_routes") == 1


def test_delete_group_with_members_deletes_only_its_exclusive_domains_and_routes(db: sqlite3.Connection) -> None:
    video, music = make_group(db, "Video"), make_group(db, "Music")
    only, both, _ = domain_ids(db, "only.com both.com free.com")
    join(db, only, video)
    join(db, both, video, music)
    route(db, "192.0.2.0/24", video)
    route(db, "198.51.100.0/24", video, music)
    route(db, "203.0.113.0/24")
    assert delete_group(db, video.id, with_members=True) == DeletedGroup(1, 1)
    assert sorted(d.canonical.name for d in list_domains(db)) == ["both.com", "free.com"]
    assert count(db, "names") == 4
    assert sorted(cidr for (cidr,) in db.execute("SELECT cidr FROM global_routes")) == ["198.51.100.0/24", "203.0.113.0/24"]
    assert group_names(db, domain(db, "both.com").id) == {"Music"}


def test_delete_unknown_group_returns_none(db: sqlite3.Connection) -> None:
    assert delete_group(db, GroupId(99), with_members=True) is None


def test_list_groups_starts_with_no_group(db: sqlite3.Connection) -> None:
    paste(db, "a.com b.com c.com")
    route(db, "192.0.2.0/24")
    assert list_groups(db) == (GroupRow(None, 3, 1),)


def test_list_groups_reads_while_another_connection_writes(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    video = make_group(db, "Video")
    with write_locked(db, other):
        assert list_groups(db) == (GroupRow(None, 0, 0), GroupRow(video, 0, 0))


def test_list_groups_counts_domains_and_routes_by_name(db: sqlite3.Connection) -> None:
    video, alpha, empty = make_group(db, "video"), make_group(db, "Alpha"), make_group(db, "Zeta")
    a, b, _ = domain_ids(db, "a.com b.com c.com")
    join(db, a, video, alpha)
    join(db, b, video)
    route(db, "192.0.2.0/24", alpha)
    route(db, "198.51.100.0/24")
    assert list_groups(db) == (GroupRow(None, 1, 1), GroupRow(alpha, 1, 1), GroupRow(empty, 0, 0), GroupRow(video, 2, 0))


def test_add_domains_to_groups_skips_unknown_ids(db: sqlite3.Connection) -> None:
    video, music = make_group(db, "Video"), make_group(db, "Music")
    a, b, c = domain_ids(db, "a.com b.com c.com")
    join(db, a, video)
    assert edit_domain_groups(db, [a, b, DomainId(99)], SetChange(add=group_ids(video, music) | {GroupId(99)}), LATER) == 2
    assert (group_names(db, a), group_names(db, b), group_names(db, c)) == ({"Video", "Music"}, {"Video", "Music"}, set())
    assert not db.in_transaction


def test_remove_domains_from_groups(db: sqlite3.Connection) -> None:
    video, music = make_group(db, "Video"), make_group(db, "Music")
    a, b = domain_ids(db, "a.com b.com")
    join(db, a, video, music)
    assert edit_domain_groups(db, [a, b], SetChange(remove=group_ids(video)), LATER) == 1
    assert (group_names(db, a), group_names(db, b)) == ({"Music"}, set())


def test_replace_groups_of_domains_then_remove_then_add(db: sqlite3.Connection) -> None:
    video, music, news = make_group(db, "Video"), make_group(db, "Music"), make_group(db, "News")
    a, b, c = domain_ids(db, "a.com b.com c.com")
    join(db, a, video)
    join(db, b, music)
    assert edit_domain_groups(db, [a, b, c], SetChange(replace=group_ids(music, news), remove=group_ids(news, video), add=group_ids(video)), LATER) == 3
    assert [group_names(db, d) for d in (a, b, c)] == [{"Music", "Video"}] * 3


def test_replacing_with_no_groups_moves_domains_to_no_group(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video")
    a, b = domain_ids(db, "a.com b.com")
    join(db, a, video)
    join(db, b, video)
    assert edit_domain_groups(db, [a, b], SetChange(replace=frozenset()), LATER) == 2
    assert list_groups(db) == (GroupRow(None, 2, 0), GroupRow(video, 0, 0))


def test_membership_edit_touches_updated_at_only_where_groups_changed(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video")
    a, b = domain_ids(db, "a.com b.com")
    join(db, a, video)
    edit_domain_groups(db, [a, b], SetChange(add=group_ids(video)), LATER)
    assert (domain(db, "a.com").updated_at, domain(db, "b.com").updated_at) == (NOW, LATER)
    assert get_group(db, video.id) == video


def test_set_group_for_a_batch(db: sqlite3.Connection) -> None:
    paste(db, "old.com")
    batch = paste(db, "a.com b.com").batch_id
    video = make_group(db, "Video")
    assert edit_domain_groups(db, [d.id for d in list_domains(db, batch)], SetChange(add=group_ids(video)), LATER) == 2
    assert list_groups(db) == (GroupRow(None, 1, 0), GroupRow(video, 2, 0))


def join_new_group_and_fail(db: sqlite3.Connection, domain_id: DomainId) -> None:
    with transaction(db):
        new = make_group(db, "New")
        edit_domain_groups(db, [domain_id], SetChange(add=group_ids(new)), LATER)
        raise RuntimeError


def test_group_edits_compose_in_one_transaction(db: sqlite3.Connection) -> None:
    (a,) = domain_ids(db, "a.com")
    with pytest.raises(RuntimeError):
        join_new_group_and_fail(db, a)
    assert (count(db, "groups"), group_names(db, a)) == (0, set())
