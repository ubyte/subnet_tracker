import sqlite3
from pathlib import Path

import pytest

from cli_world import ACTOR, NOW, Cli, Result, answering, reading, trigger_lines
from domain_view_world import add_resolver, resolve_times, resolved, seen
from domains_world import count
from groups_world import domain, make_group
from subnet_tracker.cli.output import table
from subnet_tracker.core.domain_view import domain_views
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.types import JsonValue, ReconnectMode, ResolveStatus

TIME = "2025-10-09T08:53Z"


def work(db: sqlite3.Connection) -> TickWork | None:
    claim = claim_requests(db)
    return None if claim is None else claim.work


def names(data: JsonValue) -> list[JsonValue]:
    assert isinstance(data, list)
    return [item["name"] for item in data if isinstance(item, dict)]


def test_domain_add_sets_the_settings_of_new_domains_only(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    make_group(db, "Work")
    cli("add", "old.com")
    result = cli("domain", "add", "new.com", "www.old.com", "--group", "Work", "--tag", "video", "--tag", "art", "--prefix4", "24", "--prefix6", "40", "--comment", "static")
    assert result == Result(0, "added 1 in batch 2: new.com\nskipped 1 already tracked: www.old.com\n", "")
    new, old = domain_views(db, [domain(db, "new.com").id, domain(db, "old.com").id])
    assert (new.groups, new.tags, new.domain.prefix_v4, new.domain.prefix_v6, new.domain.comment) == (("Work",), ("art", "video"), 24, 40, "static")
    assert (old.groups, old.tags, old.domain.prefix_v4) == ((), (), None)
    assert work(db) == TickWork(new_domains=frozenset({new.domain.id, old.domain.id}), apply=ReconnectMode.AUTO)
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"] * 2


def test_domain_add_reads_names_from_a_file_or_stdin(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    listing = tmp_path / "names.txt"
    listing.write_text("b.com, c.com\n\nD.com\n")
    assert cli("domain", "add", "a.com", "--from-file", str(listing)) == Result(0, "added 4 in batch 1: a.com, b.com, c.com, d.com\n", "")
    assert reading(cli, "e.com\n")("domain", "add", "--from-file", "-") == Result(0, "added 1 in batch 2: e.com\n", "")
    assert [tuple(row) for row in db.execute("SELECT kind, input FROM add_batches ORDER BY id")] == [("cli", "a.com\nb.com, c.com\n\nD.com\n"), ("cli", "e.com\n")]


def test_invalid_names_are_all_reported_and_nothing_is_written(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    listing = tmp_path / "names.txt"
    listing.write_text("b.com\nhttps://c.com/\n")
    result = cli("domain", "add", "a.com", "foo", "--from-file", str(listing))
    assert (result.code, result.out) == (2, "")
    first, second = result.err.splitlines()
    assert first == "subnet-tracker: error: 'foo': a single label is not a domain name"
    assert second.startswith(f"subnet-tracker: error: {listing}:2: 'https://c.com/': ")
    assert count(db, "add_batches") == 0


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (("--group", "Play"), "group: 'Play' does not exist"),
        (("--prefix4", "7"), "prefix_v4: prefix /7 is out of range, expected 8 to 32"),
        (("--comment", "a\nb"), "comment: line breaks are not allowed"),
    ],
)
def test_rejected_settings_write_nothing(cli: Cli, db: sqlite3.Connection, tmp_path: Path, argv: tuple[str, ...], error: str) -> None:
    assert cli("domain", "add", "a.com", *argv) == Result(2, "", f"subnet-tracker: error: {error}\n")
    assert (count(db, "add_batches"), trigger_lines(tmp_path)) == (0, [])


def test_domain_add_needs_names(cli: Cli, db: sqlite3.Connection) -> None:
    assert cli("domain", "add") == Result(2, "", "subnet-tracker: error: no names to add\n")


def test_no_resolve_requests_only_apply(cli: Cli, db: sqlite3.Connection) -> None:
    cli("domain", "add", "a.com", "--no-resolve")
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


@pytest.fixture
def listed(cli: Cli, db: sqlite3.Connection) -> Cli:
    make_group(db, "Work")
    cli("domain", "add", "a.com", "--group", "Work", "--tag", "video", "--prefix4", "24")
    cli("add", "www.b.com")
    resolve_times(db, "a.com", NOW, NOW)
    return cli


def test_list_shows_a_row_per_name(listed: Cli) -> None:
    rows = [
        ("www.b.com", "-", "-", "-", "yes", "www.b.com", "-", "-", "-"),
        ("", "", "", "", "", "b.com", "-", "-", "-"),
        ("a.com", "Work", "video", "/24", "yes", "a.com", "-", TIME, TIME),
        ("", "", "", "", "", "www.a.com", "-", "-", "-"),
    ]
    assert listed("domain", "list") == Result(0, table(("domain", "groups", "tags", "prefix", "routed", "name", "status", "success", "attempt"), rows) + "\n", "")


def test_list_json(listed: Cli) -> None:
    data = listed.json("domain", "list", "--group", "Work")
    assert data == [
        {
            "id": 1,
            "name": "a.com",
            "pair": "www.a.com",
            "groups": ["Work"],
            "tags": ["video"],
            "prefix_v4": 24,
            "prefix_v6": None,
            "enabled": True,
            "routed": True,
            "comment": "",
            "source": "manual",
            "source_url": None,
            "batch_id": 1,
            "created_at": NOW,
            "updated_at": NOW,
            "status": None,
            "names": [{"name": "a.com", "status": None, "last_attempt_at": NOW, "last_success_at": NOW}, {"name": "www.a.com", "status": None, "last_attempt_at": None, "last_success_at": None}],
        }
    ]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("--group", "none"), ["www.b.com"]),
        (("--tag", "video"), ["a.com"]),
        (("--search", "B.C"), ["www.b.com"]),
        (("--batch", "2"), ["www.b.com"]),
        (("--sort", "name"), ["a.com", "www.b.com"]),
        (("--sort", "last-attempt"), ["www.b.com", "a.com"]),
        (("--state", "disabled"), []),
    ],
)
def test_list_filters_and_sorts(listed: Cli, argv: tuple[str, ...], expected: list[str]) -> None:
    assert names(listed.json("domain", "list", *argv)) == expected


@pytest.mark.parametrize(("argv", "error"), [(("--group", "Play"), "group: 'Play' does not exist"), (("--tag", "audio"), "tag: 'audio' does not exist"), (("--batch", "9"), "batch: 9 does not exist")])
def test_list_of_an_unknown_group_tag_or_batch_is_a_usage_error(listed: Cli, argv: tuple[str, ...], error: str) -> None:
    assert listed("domain", "list", *argv) == Result(2, "", f"subnet-tracker: error: {error}\n")


@pytest.mark.parametrize("argv", [("--state", "fine"), ("--sort", "age"), ("--batch", "x")])
def test_list_rejects_bad_options(listed: Cli, argv: tuple[str, ...]) -> None:
    assert listed("domain", "list", *argv).code == 2


def test_show_by_either_name_with_results_and_addresses(listed: Cli, db: sqlite3.Connection) -> None:
    system = add_resolver(db, "system", 0)
    resolved(db, "a.com", system, ResolveStatus.OK, NOW)
    seen(db, "a.com", "192.0.2.10", system, NOW, NOW)
    expected = f"""domain   a.com
pair     www.a.com
groups   Work
tags     video
prefix   /24
enabled  yes
routed   yes
comment
source   manual, batch 1
added    {TIME}
updated  {TIME}

a.com: ok, success {TIME}, attempt {TIME}
  resolver  routes  status  success            attempt            error
  system    yes     ok      {TIME}  {TIME}
  address     state   seen by  first              last
  192.0.2.10  active  system   {TIME}  {TIME}

www.a.com: -, success -, attempt -
"""
    assert listed("domain", "show", "WWW.A.COM.") == Result(0, expected, "")


def test_show_json_has_results_and_addresses(listed: Cli, db: sqlite3.Connection) -> None:
    system = add_resolver(db, "system", 0)
    resolved(db, "a.com", system, ResolveStatus.OK, NOW)
    seen(db, "a.com", "192.0.2.10", system, NOW - 5, NOW)
    data = listed.json("domain", "show", "a.com")
    assert isinstance(data, dict)
    assert data["names"] == [
        {
            "name": "a.com",
            "status": "ok",
            "last_attempt_at": NOW,
            "last_success_at": NOW,
            "results": [{"resolver": "system", "routes": True, "status": "ok", "attempted_at": NOW, "answered_at": NOW, "succeeded_at": NOW, "error": None}],
            "addresses": [{"ip": "192.0.2.10", "state": "active", "sightings": [{"resolver": "system", "first_seen_at": NOW - 5, "last_seen_at": NOW}]}],
        },
        {"name": "www.a.com", "status": None, "last_attempt_at": None, "last_success_at": None, "results": [], "addresses": []},
    ]


@pytest.mark.parametrize(("name", "error"), [("x.com", "x.com is not tracked"), ("foo", "'foo': a single label is not a domain name")])
def test_show_of_an_unknown_or_invalid_name_is_a_usage_error(listed: Cli, name: str, error: str) -> None:
    assert listed("domain", "show", name) == Result(2, "", f"subnet-tracker: error: {error}\n")


@pytest.fixture
def two(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> Cli:
    make_group(db, "Work")
    cli("add", "a.com b.com")
    (tmp_path / "trigger").unlink()
    return cli


def test_edit_changes_every_named_domain_once(two: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    result = two("domain", "edit", "a.com", "www.a.com", "b.com", "--prefix4", "24", "--add-group", "Work", "--add-tag", "video", "--disable", "--comment", "x")
    line = 'prefix_v4 null -> 24, enabled true -> false, comment "" -> "x", groups [] -> ["Work"], tags [] -> ["video"]'
    assert result == Result(0, f"b.com: {line}\na.com: {line}\n", "")
    assert [(e.kind, e.name) for e in list_events(db, EventFilter(2))] == [(EventKind.DOMAIN_CHANGED, "a.com"), (EventKind.DOMAIN_CHANGED, "b.com")]
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]


def test_edit_sets_a_prefix_back_to_inherited_and_removes_groups_and_tags(two: Cli, db: sqlite3.Connection) -> None:
    two("domain", "edit", "a.com", "--prefix6", "40", "--add-group", "Work", "--add-tag", "video", "--enable")
    assert two("domain", "edit", "a.com", "--prefix6", "inherit", "--rm-group", "Work", "--rm-tag", "video", "--rm-tag", "audio") == Result(
        0, 'a.com: prefix_v6 40 -> null, groups ["Work"] -> [], tags ["video"] -> []\n', ""
    )


def test_edit_json(two: Cli) -> None:
    assert two.json("domain", "edit", "a.com", "--disable") == [{"id": 1, "name": "a.com", "changes": {"enabled": {"old": True, "new": False}}}]


def test_edit_that_changes_nothing_notifies_nobody(two: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert two("domain", "edit", "a.com", "--enable", "--rm-tag", "video") == Result(0, "nothing changed\n", "")
    assert (count(db, "events"), trigger_lines(tmp_path)) == (2, [])


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (("a.com",), "nothing to change, give at least one option"),
        (("a.com", "x.com", "--disable"), "x.com is not tracked"),
        (("a.com", "--rm-group", "Play"), "group: 'Play' does not exist"),
        (("a.com", "--prefix4", "33"), "prefix_v4: prefix /33 is out of range, expected 8 to 32"),
    ],
)
def test_rejected_edit_changes_nothing(two: Cli, db: sqlite3.Connection, argv: tuple[str, ...], error: str) -> None:
    assert two("domain", "edit", *argv) == Result(2, "", f"subnet-tracker: error: {error}\n")
    assert (domain(db, "a.com").enabled, count(db, "events")) == (True, 2)


@pytest.mark.parametrize("argv", [("a.com", "--enable", "--disable"), ("a.com", "--prefix4", "wide"), ("--disable",)])
def test_edit_rejects_bad_options(two: Cli, argv: tuple[str, ...]) -> None:
    assert two("domain", "edit", *argv).code == 2


def test_rm_with_yes_deletes_both_names(two: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert two("domain", "rm", "www.a.com", "b.com", "a.com", "--yes") == Result(0, "deleted 2: b.com (www.b.com), a.com (www.a.com)\n", "")
    assert count(db, "names") == 0
    assert [(e.kind, e.name) for e in list_events(db, EventFilter(2))] == [(EventKind.DOMAIN_DELETED, "a.com"), (EventKind.DOMAIN_DELETED, "b.com")]
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]


def test_rm_json(two: Cli) -> None:
    assert two.json("domain", "rm", "a.com", "--yes") == [{"id": 1, "name": "a.com", "pair": "www.a.com"}]


def test_rm_asks_on_a_terminal(two: Cli, db: sqlite3.Connection) -> None:
    prompts: list[str] = []
    assert answering(two, True, prompts)("domain", "rm", "a.com", "www.b.com") == Result(0, "deleted 2: b.com (www.b.com), a.com (www.a.com)\n", "")
    assert prompts == ["delete a.com (www.a.com), b.com (www.b.com)?"]


@pytest.mark.parametrize(("answer", "code", "error"), [(False, 1, "not confirmed, nothing deleted"), (None, 2, "stdin is not a terminal to confirm on, pass --yes to delete without asking")])
def test_rm_without_confirmation_deletes_nothing(two: Cli, db: sqlite3.Connection, tmp_path: Path, answer: bool | None, code: int, error: str) -> None:
    assert answering(two, answer, [])("domain", "rm", "a.com") == Result(code, "", f"subnet-tracker: error: {error}\n")
    assert (count(db, "domains"), trigger_lines(tmp_path)) == (2, [])


def test_rm_of_an_unknown_name_deletes_nothing(two: Cli, db: sqlite3.Connection) -> None:
    assert two("domain", "rm", "a.com", "x.com", "--yes") == Result(2, "", "subnet-tracker: error: x.com is not tracked\n")
    assert count(db, "domains") == 2
