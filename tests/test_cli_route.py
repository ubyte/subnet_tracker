import json
import sqlite3
from pathlib import Path

from cli_world import ACTOR, NOW, Cli, Result, trigger_lines
from domains_world import count
from groups_world import make_group
from subnet_tracker.cli.output import table
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.types import ReconnectMode


def work(db: sqlite3.Connection) -> TickWork | None:
    claim = claim_requests(db)
    return None if claim is None else claim.work


def test_add_normalizes_and_requests_apply(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    make_group(db, "Video")
    assert cli("route", "add", "192.0.2.7/24", "--group", "Video", "--comment", "LAN") == Result(0, "added route 192.0.2.0/24\n", "")
    rows = [("192.0.2.0/24", "Video", "LAN", "yes")]
    assert cli("route", "list") == Result(0, table(("cidr", "groups", "comment", "enabled"), rows) + "\n", "")
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]
    assert [e.kind for e in list_events(db, EventFilter(1))] == [EventKind.ROUTE_ADDED]


def test_rejected_cidr_and_unknown_group_write_nothing(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    cli.config.write_text(f'db = {json.dumps(str(tmp_path / "state.db"))}\n[routes]\nserver_addresses = ["203.0.113.10"]\n', encoding="utf-8")
    assert cli("route", "add", "203.0.113.10") == Result(2, "", "subnet-tracker: error: cidr: covers the server address 203.0.113.10\n")
    assert cli("route", "add", "::ffff:192.0.2.1") == Result(2, "", "subnet-tracker: error: cidr: IPv4-mapped addresses are not routable, use the IPv4 form\n")
    assert cli("route", "add", "192.0.2.0/24", "--group", "Missing") == Result(2, "", "subnet-tracker: error: group: 'Missing' does not exist\n")
    assert (count(db, "global_routes"), trigger_lines(tmp_path)) == (0, [])


def test_edit_comment_does_not_request_apply_and_groups_do(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    make_group(db, "Video")
    cli("route", "add", "192.0.2.0/24")
    (tmp_path / "trigger").unlink()
    before = count(db, "requests")
    assert cli("route", "edit", "192.0.2.9/24", "--comment", "LAN") == Result(0, '192.0.2.0/24: comment "" -> "LAN"\n', "")
    assert count(db, "requests") == before
    assert trigger_lines(tmp_path) == []
    assert cli("route", "edit", "192.0.2.0/24", "--add-group", "Video", "--disable") == Result(0, '192.0.2.0/24: enabled true -> false, groups [] -> ["Video"]\n', "")
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]


def test_edit_without_options_or_an_unknown_route_writes_nothing(cli: Cli, db: sqlite3.Connection) -> None:
    cli("route", "add", "192.0.2.0/24")
    assert cli("route", "edit", "192.0.2.0/24") == Result(2, "", "subnet-tracker: error: nothing to change, give at least one option\n")
    assert cli("route", "rm", "198.51.100.0/24") == Result(2, "", "subnet-tracker: error: cidr: 198.51.100.0/24 does not exist\n")
    assert count(db, "global_routes") == 1


def test_rm_deletes_and_requests_apply(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    cli("route", "add", "192.0.2.7")
    (tmp_path / "trigger").unlink()
    assert cli("route", "rm", "192.0.2.7/32") == Result(0, "deleted route 192.0.2.7/32\n", "")
    assert cli("route", "list") == Result(0, table(("cidr", "groups", "comment", "enabled"), []) + "\n", "")
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]
