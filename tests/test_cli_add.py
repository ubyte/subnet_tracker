import json
import sqlite3
from pathlib import Path

from cli_world import ACTOR, NOW, Cli, Result, reading, trigger_lines
from domains_world import count
from groups_world import domain
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.types import ReconnectMode


def test_add_prints_the_summary_and_hands_new_domains_to_tick(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    result = cli("add", "a.com", "https://www.b.com/x foo", "1.2.3.4/24", "*.c.com")
    assert result == Result(
        3,
        "added 3 in batch 1: a.com, www.b.com, c.com\n"
        "not recognized: line 2: 'foo': a single label is not a domain name\n"
        "networks, add them with 'subnet-tracker route add': 1.2.3.0/24\n"
        "wildcards are not supported, added without '*.': c.com\n",
        "",
    )
    ids = frozenset(domain(db, name).id for name in ("a.com", "b.com", "c.com"))
    claim = claim_requests(db)
    assert claim is not None
    assert claim.work == TickWork(new_domains=ids, apply=ReconnectMode.AUTO)
    assert [(e.kind, e.actor) for e in list_events(db, EventFilter(10))] == [(EventKind.DOMAIN_ADDED, ACTOR)] * 3
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]
    assert db.execute("SELECT kind, input FROM add_batches").fetchone()[:] == ("cli", "a.com\nhttps://www.b.com/x foo\n1.2.3.4/24\n*.c.com")


def test_add_reads_stdin_for_a_dash(cli: Cli, db: sqlite3.Connection) -> None:
    assert reading(cli, "a.com\nwww.a.com b.com\n")("add", "-") == Result(0, "added 2 in batch 1: a.com, b.com\nskipped 1 already tracked: www.a.com\n", "")


def test_dash_goes_alone(cli: Cli, db: sqlite3.Connection) -> None:
    assert cli("add", "-", "a.com") == Result(2, "", "subnet-tracker: error: '-' reads the text from stdin and goes alone\n")
    assert count(db, "add_batches") == 0


def test_empty_text_is_a_usage_error(cli: Cli, db: sqlite3.Connection) -> None:
    assert reading(cli, " \n, ")("add", "-") == Result(2, "", "subnet-tracker: error: nothing to add\n")
    assert count(db, "add_batches") == 0


def test_add_needs_text(cli: Cli, db: sqlite3.Connection) -> None:
    assert cli("add").code == 2


def test_only_duplicates_record_the_batch_and_notify_nobody(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    cli("add", "a.com")
    assert cli("add", "www.a.com") == Result(0, "added 0 in batch 2\nskipped 1 already tracked: www.a.com\n", "")
    assert (count(db, "add_batches"), count(db, "requests"), len(trigger_lines(tmp_path))) == (2, 2, 1)


def test_more_than_50_domains_are_counted_only(cli: Cli, db: sqlite3.Connection) -> None:
    cli("add", "a0.com")
    result = cli("add", *(f"a{i}.com" for i in range(52)))
    assert result == Result(0, "added 51 in batch 2\nskipped 1 already tracked: a0.com\n", "")


def test_add_json(cli: Cli, db: sqlite3.Connection) -> None:
    result = cli("add", "a.com www.a.com 10.0.0.1 *.b.com", "x", "--json")
    assert (result.code, result.err) == (3, "")
    assert json.loads(result.out) == {
        "batch_id": 1,
        "added": ["a.com", "b.com"],
        "duplicates": ["www.a.com"],
        "invalid": [{"line": 2, "message": "'x': a single label is not a domain name"}],
        "networks": ["10.0.0.1/32"],
        "wildcards": ["b.com"],
    }
