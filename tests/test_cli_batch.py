import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from cli_world import ACTOR, NOW, Cli, Result, trigger_lines
from domains_world import count
from subnet_tracker.cli.output import table
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.types import Timestamp

TIME = "2025-10-09T08:53Z"


@pytest.fixture
def added(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> Cli:
    cli("add", "a.com b.com", "foo")
    replace(cli, runtime=replace(cli.runtime, clock=lambda: Timestamp(NOW + 60)))("add", "www.a.com")
    (tmp_path / "trigger").unlink()
    return cli


def test_list_shows_batches_newest_first(added: Cli) -> None:
    rows = [("2", "2025-10-09T08:54Z", "cli", ACTOR, "0", "1", "0", "-"), ("1", TIME, "cli", ACTOR, "2", "0", "1", "-")]
    assert added("batch", "list") == Result(0, table(("id", "created", "kind", "actor", "added", "duplicates", "invalid", "undone"), rows) + "\n", "")


def test_list_limit_and_json(added: Cli) -> None:
    assert added.json("batch", "list", "--limit", "1") == [{"id": 2, "created_at": NOW + 60, "kind": "cli", "actor": ACTOR, "added": 0, "duplicates": 1, "invalid": [], "undone_at": None}]


@pytest.mark.parametrize("limit", ["0", "-1", "x"])
def test_list_limit_is_a_positive_number(added: Cli, limit: str) -> None:
    assert added("batch", "list", "--limit", limit).code == 2


def test_show_prints_the_batch_with_its_remaining_domains(added: Cli) -> None:
    expected = f"batch 1: cli by {ACTOR} at {TIME}\nadded 2, skipped 0, not recognized 1\nnot recognized: line 2: 'foo': a single label is not a domain name\ndomains: a.com, b.com\n"
    assert added("batch", "show", "1") == Result(0, expected, "")


def test_show_json(added: Cli) -> None:
    assert added.json("batch", "show", "1") == {
        "id": 1,
        "created_at": NOW,
        "kind": "cli",
        "actor": ACTOR,
        "added": 2,
        "duplicates": 0,
        "invalid": [{"line": 2, "message": "'foo': a single label is not a domain name"}],
        "undone_at": None,
        "input": "a.com b.com\nfoo",
        "domains": ["a.com", "b.com"],
    }


def test_undo_deletes_the_remaining_domains(added: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert added("batch", "undo", "1") == Result(0, "batch 1 undone, deleted 2: a.com, b.com\n", "")
    assert count(db, "domains") == 0
    assert [(e.kind, e.name, e.details) for e in list_events(db, EventFilter(2))] == [(EventKind.DOMAIN_DELETED, "b.com", {"batch_id": 1}), (EventKind.DOMAIN_DELETED, "a.com", {"batch_id": 1})]
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]
    assert (
        added("batch", "show", "1").out
        == f"batch 1: cli by {ACTOR} at {TIME}, undone {TIME}\nadded 2, skipped 0, not recognized 1\nnot recognized: line 2: 'foo': a single label is not a domain name\ndomains: -\n"
    )


def test_undo_again_deletes_nothing_and_notifies_nobody(added: Cli, tmp_path: Path) -> None:
    added("batch", "undo", "1")
    assert added("batch", "undo", "1") == Result(0, "batch 1 undone, deleted 0\n", "")
    assert len(trigger_lines(tmp_path)) == 1


def test_undo_json(added: Cli) -> None:
    assert added.json("batch", "undo", "1") == {"batch_id": 1, "deleted": ["a.com", "b.com"]}


@pytest.mark.parametrize("command", ["show", "undo"])
def test_unknown_batch_is_a_usage_error(added: Cli, command: str) -> None:
    assert added("batch", command, "9") == Result(2, "", "subnet-tracker: error: batch 9 does not exist\n")
