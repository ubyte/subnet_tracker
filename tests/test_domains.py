import sqlite3

import pytest

from domains_world import NOW, count, dn, paste
from subnet_tracker.core.domains import Domain, DomainEdit, checked_edit, delete_domains, edit_domains, find_domain, get_domain, list_domains
from subnet_tracker.core.names import Name
from subnet_tracker.types import DomainId, DomainName, DomainSource, FieldError, Timestamp


def only(db: sqlite3.Connection, name: str) -> Domain:
    domain = find_domain(db, DomainName(name))
    assert domain is not None
    return domain


def test_list_domains_newest_first_with_both_names(db: sqlite3.Connection) -> None:
    first = paste(db, "a.com")
    second = paste(db, "www.b.com", Timestamp(NOW + 1))
    domains = list_domains(db)
    assert [(d.canonical.name, d.pair.name) for d in domains] == [("www.b.com", "b.com"), ("a.com", "www.a.com")]
    b = domains[0]
    assert b == Domain(b.id, b.canonical, b.pair, None, None, True, "", DomainSource.MANUAL, None, second.batch_id, Timestamp(NOW + 1), Timestamp(NOW + 1))
    assert (b.canonical.domain_id, b.canonical.canonical, b.pair.domain_id, b.pair.canonical) == (b.id, True, b.id, False)
    assert b.names == (b.canonical, b.pair)
    assert domains[1].batch_id == first.batch_id


def test_list_domains_of_one_batch(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    second = paste(db, "b.com c.com")
    assert [d.canonical.name for d in list_domains(db, second.batch_id)] == ["c.com", "b.com"]


def test_name_resolve_times_are_read(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    db.execute("UPDATE names SET last_attempt_at = 20, last_success_at = 10 WHERE name = 'www.a.com'")
    domain = only(db, "a.com")
    assert domain.pair == Name(domain.pair.id, domain.id, DomainName("www.a.com"), False, Timestamp(20), Timestamp(10))
    assert (domain.canonical.last_attempt_at, domain.canonical.last_success_at) == (None, None)


def test_find_domain_by_either_name(db: sqlite3.Connection) -> None:
    paste(db, "www.b.com a.com")
    assert only(db, "b.com") == only(db, "www.b.com")
    assert only(db, "b.com").canonical.name == "www.b.com"
    assert find_domain(db, DomainName("c.com")) is None


def test_get_domain_by_id(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    domain = only(db, "a.com")
    assert get_domain(db, domain.id) == domain
    assert get_domain(db, DomainId(domain.id + 1)) is None


def test_delete_removes_domain_with_both_names_and_their_addresses(db: sqlite3.Connection) -> None:
    paste(db, "a.com b.com")
    a = only(db, "a.com")
    for name in a.names:
        db.execute("INSERT INTO addresses (name_id, ip, family, ip_bin) VALUES (?, '192.0.2.1', 4, ?)", (name.id, bytes(4)))
    assert delete_domains(db, [a.id]) == 1
    assert [d.canonical.name for d in list_domains(db)] == ["b.com"]
    assert (count(db, "names"), count(db, "addresses")) == (2, 0)


def test_delete_many_and_unknown(db: sqlite3.Connection) -> None:
    paste(db, "a.com b.com c.com")
    assert delete_domains(db, [only(db, "a.com").id, only(db, "www.c.com").id, DomainId(99)]) == 2
    assert [d.canonical.name for d in list_domains(db)] == ["b.com"]
    assert delete_domains(db, []) == 0


def test_edit_sets_every_field(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    a = only(db, "a.com")
    assert edit_domains(db, [a.id], DomainEdit(prefix_v4=24, prefix_v6=40, enabled=False, comment="  static content "), Timestamp(NOW + 1)) == 1
    edited = only(db, "a.com")
    assert (edited.prefix_v4, edited.prefix_v6, edited.enabled, edited.comment, edited.created_at, edited.updated_at) == (24, 40, False, "static content", NOW, NOW + 1)


def test_edit_keeps_fields_it_does_not_mention(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    a = only(db, "a.com")
    edit_domains(db, [a.id], DomainEdit(prefix_v4=24, prefix_v6=40, enabled=False, comment="c"), NOW)
    edit_domains(db, [a.id], DomainEdit(prefix_v4=None, enabled=True), Timestamp(NOW + 1))
    edited = only(db, "a.com")
    assert (edited.prefix_v4, edited.prefix_v6, edited.enabled, edited.comment) == (None, 40, True, "c")


def test_edit_many_domains(db: sqlite3.Connection) -> None:
    paste(db, "a.com b.com c.com")
    assert edit_domains(db, [only(db, "a.com").id, only(db, "b.com").id, DomainId(99)], DomainEdit(enabled=False), NOW) == 2
    assert {d.canonical.name: d.enabled for d in list_domains(db)} == {"a.com": False, "b.com": False, "c.com": True}


def test_empty_edit_changes_nothing(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    a = only(db, "a.com")
    assert edit_domains(db, [a.id], DomainEdit(), Timestamp(NOW + 1)) == 0
    assert only(db, "a.com") == a


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (DomainEdit(prefix_v4=7), FieldError("prefix_v4", "prefix /7 is out of range, expected 8 to 32")),
        (DomainEdit(prefix_v4=33), FieldError("prefix_v4", "prefix /33 is out of range, expected 8 to 32")),
        (DomainEdit(prefix_v6=15), FieldError("prefix_v6", "prefix /15 is out of range, expected 16 to 128")),
        (DomainEdit(prefix_v6=129), FieldError("prefix_v6", "prefix /129 is out of range, expected 16 to 128")),
        (DomainEdit(comment="line\nbreak"), FieldError("comment", "line breaks are not allowed")),
        (DomainEdit(enabled=False, comment="trailing\r\n"), FieldError("comment", "line breaks are not allowed")),
    ],
)
def test_invalid_edit_is_rejected_and_changes_nothing(db: sqlite3.Connection, edit: DomainEdit, error: FieldError) -> None:
    assert checked_edit(edit) == error
    paste(db, "a.com")
    a = only(db, "a.com")
    assert edit_domains(db, [a.id], edit, Timestamp(NOW + 1)) == error
    assert only(db, "a.com") == a


@pytest.mark.parametrize(
    ("edit", "checked"),
    [
        (DomainEdit(), DomainEdit()),
        (DomainEdit(comment=" x "), DomainEdit(comment="x")),
        (DomainEdit(prefix_v4=None, prefix_v6=128, enabled=False), DomainEdit(prefix_v4=None, prefix_v6=128, enabled=False)),
    ],
)
def test_checked_edit_normalizes_valid_edit(edit: DomainEdit, checked: DomainEdit) -> None:
    assert checked_edit(edit) == checked


def test_duplicates_leave_domain_list_unchanged(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    before = list_domains(db)
    assert paste(db, "www.a.com a.com", Timestamp(NOW + 1)).duplicates == dn("www.a.com", "a.com")
    assert list_domains(db) == before
