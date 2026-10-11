import sqlite3
from pathlib import Path

from cli_world import ACTOR, NOW, Cli, Result, answering, trigger_lines
from domains_world import count, paste
from groups_world import domain, join
from subnet_tracker.cli.output import table
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.groups import find_group
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.types import ReconnectMode


def work(db: sqlite3.Connection) -> TickWork | None:
    claim = claim_requests(db)
    return None if claim is None else claim.work


def test_help_lists_group_tag_route_and_resolver(cli: Cli) -> None:
    result = cli("--help")
    assert result.code == 0
    assert all(name in result.out for name in ("group", "tag", "route", "resolver"))


def test_list_starts_with_the_row_without_a_group(cli: Cli, db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    cli("group", "add", "Video", "--description", "hosting", "--prefix4", "24", "--prefix6", "40")
    rows = [("none", "-", "1", "0", "-", "-", "-"), ("Video", "hosting", "0", "0", "/24", "/40", "yes")]
    assert cli("group", "list") == Result(0, table(("name", "description", "domains", "routes", "prefix4", "prefix6", "enabled"), rows) + "\n", "")
    data = cli.json("group", "list")
    assert isinstance(data, list)
    assert data[0]["name"] is None
    assert data[0]["domains"] == 1
    assert data[1]["name"] == "Video"
    assert data[1]["prefix_v4"] == 24


def test_add_trims_the_name_and_notifies_without_apply(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert cli("group", "add", " Video ") == Result(0, "added group Video\n", "")
    assert work(db) is None
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]
    assert [e.kind for e in list_events(db, EventFilter(1))] == [EventKind.GROUP_ADDED]


def test_reserved_and_invalid_names_are_usage_errors(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert cli("group", "add", "NONE") == Result(2, "", "subnet-tracker: error: name: 'NONE' is reserved for domains without a group\n")
    assert cli("group", "add", "Video", "--prefix4", "7") == Result(2, "", "subnet-tracker: error: prefix_v4: prefix /7 is out of range, expected 8 to 32\n")
    assert (count(db, "groups"), trigger_lines(tmp_path)) == (0, [])


def test_edit_prefix_requests_apply_and_a_description_does_not(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    cli("group", "add", "Video")
    (tmp_path / "trigger").unlink()
    assert cli("group", "edit", "Video", "--description", "hosting") == Result(0, 'Video: description "" -> "hosting"\n', "")
    assert work(db) is None
    assert cli("group", "edit", "Video", "--prefix4", "24", "--disable") == Result(0, "Video: prefix_v4 null -> 24, enabled true -> false\n", "")
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)
    assert cli("group", "edit", "Video", "--prefix4", "inherit") == Result(0, "Video: prefix_v4 24 -> null\n", "")


def test_edit_that_changes_nothing_notifies_nobody(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    cli("group", "add", "Video")
    (tmp_path / "trigger").unlink()
    assert cli("group", "edit", "Video", "--enable", "--description", "") == Result(0, "nothing changed\n", "")
    assert (count(db, "events"), trigger_lines(tmp_path)) == (1, [])


def test_edit_without_options_or_of_an_unknown_group_writes_nothing(cli: Cli, db: sqlite3.Connection) -> None:
    cli("group", "add", "Video")
    assert cli("group", "edit", "Video") == Result(2, "", "subnet-tracker: error: nothing to change, give at least one option\n")
    assert cli("group", "edit", "Missing", "--disable") == Result(2, "", "subnet-tracker: error: name: 'Missing' does not exist\n")
    assert cli("group", "edit", "Video", "--name", "none") == Result(2, "", "subnet-tracker: error: name: 'none' is reserved for domains without a group\n")


def test_rm_with_yes_leaves_members_and_requests_apply(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    cli("group", "add", "Video")
    paste(db, "a.com")
    group = find_group(db, "Video")
    assert group is not None
    join(db, domain(db, "a.com").id, group)
    (tmp_path / "trigger").unlink()
    assert cli("group", "rm", "Video", "--yes") == Result(0, "deleted group Video\n", "")
    assert domain(db, "a.com").updated_at == NOW
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]
    data = cli.json("group", "list")
    assert isinstance(data, list)
    assert data[0]["domains"] == 1


def test_rm_with_members_deletes_exclusive_domains_and_routes(cli: Cli, db: sqlite3.Connection) -> None:
    cli("group", "add", "Video")
    cli("group", "add", "Music")
    paste(db, "only.com both.com")
    video, music = find_group(db, "Video"), find_group(db, "Music")
    assert video is not None
    assert music is not None
    join(db, domain(db, "only.com").id, video)
    join(db, domain(db, "both.com").id, video, music)
    cli("route", "add", "192.0.2.0/24", "--group", "Video")
    assert cli("group", "rm", "Video", "--with-members", "--yes") == Result(
        0, "deleted group Video\ndeleted 1 domain that belonged only to it: only.com\ndeleted 1 route that belonged only to it: 192.0.2.0/24\n", ""
    )
    assert count(db, "domains") == 1
    assert [e.kind for e in list_events(db, EventFilter(3))][:3] == [EventKind.GROUP_DELETED, EventKind.ROUTE_DELETED, EventKind.DOMAIN_DELETED]


def test_rm_asks_on_a_terminal_and_refuses_without_one(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    cli("group", "add", "Video")
    prompts: list[str] = []
    assert answering(cli, None, prompts)("group", "rm", "Video") == Result(2, "", "subnet-tracker: error: stdin is not a terminal to confirm on, pass --yes to delete without asking\n")
    assert answering(cli, False, prompts)("group", "rm", "Video") == Result(1, "", "subnet-tracker: error: not confirmed, nothing deleted\n")
    assert prompts == ["delete group Video?"] * 2
    assert find_group(db, "Video") is not None
    assert answering(cli, True, prompts)("group", "rm", "Video", "--with-members") == Result(0, "deleted group Video\n", "")
    assert prompts[-1] == "delete group Video and the domains and routes that belong only to it?"
    assert answering(cli, True, [])("group", "rm", "Missing") == Result(2, "", "subnet-tracker: error: name: 'Missing' does not exist\n")
    assert trigger_lines(tmp_path)[-1:] == [f"{NOW} {ACTOR}"]
