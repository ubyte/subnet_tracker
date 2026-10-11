import json
import sqlite3
from ipaddress import ip_network

import pytest

from domains_world import ACTOR, NOW, count, dn, paste
from subnet_tracker.core.batches import MAX_INPUT_BYTES, Batch, add_names, add_text, clip_input, get_batch, list_batches, undo_batch
from subnet_tracker.core.domains import delete_domains, find_domain, list_domains
from subnet_tracker.discovery.www import domain_names
from subnet_tracker.types import BatchId, BatchKind, DomainName, DomainSource, ParseError, Timestamp


def domains_of(names: tuple[DomainName, ...]) -> tuple[object, ...]:
    return tuple(domain_names(n) for n in names)


def name_rows(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [tuple(r) for r in db.execute("SELECT d.batch_id, d.source, d.created_at, d.updated_at, n.name, n.canonical FROM domains d JOIN names n ON n.domain_id = d.id ORDER BY n.id")]


def tracked(db: sqlite3.Connection) -> set[str]:
    return {name for (name,) in db.execute("SELECT name FROM names")}


def test_paste_creates_batch_and_domains_with_both_names(db: sqlite3.Connection) -> None:
    text = "https://www.Static.ex.net/x example.com"
    result = paste(db, text)
    assert result.added == domains_of(dn("www.static.ex.net", "example.com"))
    assert (result.duplicates, result.invalid, result.networks, result.wildcards) == ((), (), (), ())
    b = result.batch_id
    assert name_rows(db) == [
        (b, "manual", NOW, NOW, "www.static.ex.net", 1),
        (b, "manual", NOW, NOW, "static.ex.net", 0),
        (b, "manual", NOW, NOW, "example.com", 1),
        (b, "manual", NOW, NOW, "www.example.com", 0),
    ]
    row = db.execute("SELECT * FROM add_batches").fetchone()
    assert dict(row) == {"id": b, "created_at": NOW, "kind": "paste", "actor": ACTOR, "input": text, "added": 2, "duplicates": 0, "invalid": "[]", "undone_at": None}
    assert not db.in_transaction


@pytest.mark.parametrize(("text", "canonical", "duplicate"), [("example.com www.example.com", "example.com", "www.example.com"), ("www.example.com example.com", "www.example.com", "example.com")])
def test_name_and_its_pair_in_one_paste_give_one_domain(db: sqlite3.Connection, text: str, canonical: str, duplicate: str) -> None:
    result = paste(db, text)
    assert result.added == domains_of(dn(canonical))
    assert result.duplicates == dn(duplicate)
    assert [(name, flag) for *_, name, flag in name_rows(db)] == [(canonical, 1), (domain_names(DomainName(canonical)).pair, 0)]


def test_duplicates_by_either_name_are_skipped_and_do_not_change_existing(db: sqlite3.Connection) -> None:
    first = paste(db, "example.com www.youtube.com")
    db.execute("UPDATE domains SET comment = 'kept', prefix_v4 = 24")
    second = paste(db, "www.example.com youtube.com new.com", Timestamp(NOW + 1))
    assert second.added == domains_of(dn("new.com"))
    assert second.duplicates == dn("www.example.com", "youtube.com")
    old = [tuple(r) for r in db.execute("SELECT batch_id, comment, prefix_v4, updated_at FROM domains WHERE batch_id = ?", (first.batch_id,))]
    assert old == [(first.batch_id, "kept", 24, NOW)] * 2
    assert tuple(db.execute("SELECT added, duplicates FROM add_batches WHERE id = ?", (second.batch_id,)).fetchone()) == (1, 2)
    assert count(db, "names") == 6


def test_batch_of_duplicates_only_is_still_recorded(db: sqlite3.Connection) -> None:
    paste(db, "example.com")
    result = paste(db, "www.example.com")
    assert (result.added, result.duplicates) == ((), dn("www.example.com"))
    assert tuple(db.execute("SELECT added, duplicates FROM add_batches WHERE id = ?", (result.batch_id,)).fetchone()) == (0, 1)


def test_parse_errors_are_stored_as_line_and_message(db: sqlite3.Connection) -> None:
    result = paste(db, "foo\nexample.com 1.2.3.400")
    errors = (ParseError(1, "'foo': a single label is not a domain name"), ParseError(2, "'1.2.3.400': numeric top-level label"))
    assert result.invalid == errors
    assert json.loads(db.execute("SELECT invalid FROM add_batches").fetchone()[0]) == [[e.line, e.message] for e in errors]
    summary = get_batch(db, result.batch_id)
    assert summary is not None
    assert summary.batch.invalid == errors


def test_networks_and_wildcards_are_reported_not_added(db: sqlite3.Connection) -> None:
    result = paste(db, "1.2.3.4/24 *.example.com")
    assert result.networks == (ip_network("1.2.3.0/24"),)
    assert result.wildcards == dn("example.com")
    assert result.added == domains_of(dn("example.com"))
    assert tracked(db) == {"example.com", "www.example.com"}


@pytest.mark.parametrize(
    ("kind", "source"), [(BatchKind.PASTE, DomainSource.MANUAL), (BatchKind.CLI, DomainSource.MANUAL), (BatchKind.SCAN, DomainSource.DISCOVERED), (BatchKind.IMPORT, DomainSource.IMPORT)]
)
def test_domain_source_follows_batch_kind(db: sqlite3.Connection, kind: BatchKind, source: DomainSource) -> None:
    for result in (add_text(db, kind, "cli:root", "a.com", NOW), add_names(db, kind, "cli:root", "b.com", dn("b.com"), NOW)):
        assert db.execute("SELECT kind FROM add_batches WHERE id = ?", (result.batch_id,)).fetchone()[0] == kind
    assert {s for (s,) in db.execute("SELECT source FROM domains")} == {source}


def test_add_names_skips_tracked_names_and_repeats(db: sqlite3.Connection) -> None:
    paste(db, "example.com")
    result = add_names(db, BatchKind.SCAN, ACTOR, "https://example.com/", dn("www.example.com", "cdn.example.com", "www.cdn.example.com"), NOW)
    assert result.added == domains_of(dn("cdn.example.com"))
    assert result.duplicates == dn("www.example.com", "www.cdn.example.com")
    assert dict(db.execute("SELECT input, added, duplicates, invalid FROM add_batches WHERE id = ?", (result.batch_id,)).fetchone()) == {
        "input": "https://example.com/",
        "added": 1,
        "duplicates": 2,
        "invalid": "[]",
    }


def test_batch_is_written_in_one_transaction(db: sqlite3.Connection) -> None:
    db.execute("CREATE TEMP TRIGGER boom BEFORE INSERT ON names WHEN NEW.name = 'www.boom.com' BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        paste(db, "a.com boom.com")
    assert (count(db, "add_batches"), count(db, "domains"), count(db, "names")) == (0, 0, 0)
    assert not db.in_transaction


@pytest.mark.parametrize(
    ("text", "clipped"),
    [
        ("", ""),
        ("example.com", "example.com"),
        ("a" * MAX_INPUT_BYTES, "a" * MAX_INPUT_BYTES),
        ("a" * MAX_INPUT_BYTES + "b", "a" * MAX_INPUT_BYTES),
        ("a" * (MAX_INPUT_BYTES - 1) + "я", "a" * (MAX_INPUT_BYTES - 1)),
    ],
)
def test_clip_input_keeps_64_kb_of_whole_characters(text: str, clipped: str) -> None:
    assert MAX_INPUT_BYTES == 64 * 1024
    assert clip_input(text) == clipped


def test_long_input_is_clipped_but_fully_added(db: sqlite3.Connection) -> None:
    names = [f"d{i}.com" for i in range(7000)]
    result = paste(db, "\n".join(names))
    assert len(result.added) == 7000
    stored = db.execute("SELECT input FROM add_batches").fetchone()[0]
    assert stored == clip_input("\n".join(names))
    assert len(stored.encode()) == MAX_INPUT_BYTES


def test_summary_lists_remaining_domains_in_input_order(db: sqlite3.Connection) -> None:
    result = paste(db, "b.com a.com c.com")
    a = find_domain(db, DomainName("a.com"))
    assert a is not None
    delete_domains(db, [a.id])
    summary = get_batch(db, result.batch_id)
    assert summary is not None
    assert summary.batch == Batch(result.batch_id, NOW, BatchKind.PASTE, ACTOR, "b.com a.com c.com", 3, 0, (), None)
    assert [d.canonical.name for d in summary.domains] == ["b.com", "c.com"]
    assert summary.domains == tuple(reversed(list_domains(db, result.batch_id)))


def test_get_unknown_batch(db: sqlite3.Connection) -> None:
    assert get_batch(db, BatchId(99)) is None


def test_list_batches_newest_first(db: sqlite3.Connection) -> None:
    ids = [paste(db, f"d{i}.com", Timestamp(NOW + i)).batch_id for i in range(3)]
    assert [b.id for b in list_batches(db, limit=2)] == [ids[2], ids[1]]
    assert [b.created_at for b in list_batches(db, limit=10)] == [NOW + 2, NOW + 1, NOW]


def test_undo_removes_only_remaining_domains_of_the_batch(db: sqlite3.Connection) -> None:
    first = paste(db, "a.com b.com c.com")
    paste(db, "d.com")
    b = find_domain(db, DomainName("b.com"))
    assert b is not None
    delete_domains(db, [b.id])
    paste(db, "www.b.com")
    assert undo_batch(db, first.batch_id, Timestamp(NOW + 5)) == dn("a.com", "c.com")
    assert tracked(db) == {"d.com", "www.d.com", "www.b.com", "b.com"}
    summary = get_batch(db, first.batch_id)
    assert summary is not None
    assert (summary.batch.undone_at, summary.domains) == (NOW + 5, ())
    assert not db.in_transaction


def test_undo_removes_addresses_of_both_names(db: sqlite3.Connection) -> None:
    result = paste(db, "a.com")
    for name_id, ip in db.execute("SELECT id, '192.0.2.' || id FROM names").fetchall():
        db.execute("INSERT INTO addresses (name_id, ip, family, ip_bin) VALUES (?, ?, 4, ?)", (name_id, ip, bytes(4)))
    undo_batch(db, result.batch_id, NOW)
    assert (count(db, "names"), count(db, "addresses")) == (0, 0)


def test_second_undo_keeps_first_time(db: sqlite3.Connection) -> None:
    result = paste(db, "a.com")
    undo_batch(db, result.batch_id, Timestamp(NOW + 1))
    assert undo_batch(db, result.batch_id, Timestamp(NOW + 2)) == ()
    assert db.execute("SELECT undone_at FROM add_batches").fetchone()[0] == NOW + 1


def test_undone_domains_can_be_added_again(db: sqlite3.Connection) -> None:
    result = paste(db, "a.com")
    undo_batch(db, result.batch_id, NOW)
    assert paste(db, "a.com").added == domains_of(dn("a.com"))


def test_undo_unknown_batch(db: sqlite3.Connection) -> None:
    assert undo_batch(db, BatchId(99), NOW) is None
    assert count(db, "add_batches") == 0
