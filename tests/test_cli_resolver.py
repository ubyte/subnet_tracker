import sqlite3
from contextlib import closing
from pathlib import Path

from cli_world import ACTOR, NOW, Cli, Result, trigger_lines
from domains_world import count
from subnet_tracker.cli.output import table
from subnet_tracker.core.db import connect
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.core.resolvers import DEFAULT_RESOLVERS
from subnet_tracker.types import ReconnectMode


def work(db: sqlite3.Connection) -> TickWork | None:
    claim = claim_requests(db)
    return None if claim is None else claim.work


def test_list_shows_the_resolvers_created_by_migrate(cli: Cli, tmp_path: Path) -> None:
    assert cli("db", "migrate").code == 0
    text = cli("resolver", "list")
    assert text.code == 0
    assert all(resolver.name in text.out for resolver in DEFAULT_RESOLVERS)
    data = cli.json("resolver", "list")
    assert isinstance(data, list)
    assert [item["name"] for item in data if isinstance(item, dict)] == [resolver.name for resolver in DEFAULT_RESOLVERS]
    added = cli.json("resolver", "add", "Quad9", "--kind", "dns", "--endpoint", "udp://9.9.9.9")
    assert isinstance(added, dict)
    assert added["position"] == 6
    with closing(connect(tmp_path / "state.db")) as conn:
        assert claim_requests(conn) is None
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]


def test_add_edit_and_delete(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert cli("resolver", "add", "Quad9", "--kind", "dns", "--endpoint", "UDP://9.9.9.9", "--no-routes", "--max-qps", "20") == Result(0, "added resolver Quad9\n", "")
    rows = [("no", "Quad9", "dns", "udp://9.9.9.9", "20")]
    assert cli("resolver", "list") == Result(0, table(("routes", "name", "kind", "endpoint", "qps"), rows) + "\n", "")
    assert work(db) is None
    (tmp_path / "trigger").unlink()
    assert cli("resolver", "edit", "Quad9", "--max-qps", "10") == Result(0, "Quad9: max_qps 20 -> 10\n", "")
    assert work(db) is None
    assert cli("resolver", "edit", "Quad9", "--routes") == Result(0, "Quad9: use_for_routes false -> true\n", "")
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)
    assert [e.kind for e in list_events(db, EventFilter(1))] == [EventKind.RESOLVER_CHANGED]
    assert cli("resolver", "rm", "Quad9") == Result(0, "deleted resolver Quad9\n", "")
    assert count(db, "resolvers") == 0


def test_agent_kind_and_bad_endpoint_are_usage_errors(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert cli("resolver", "add", "laptop", "--kind", "agent").code == 2
    assert cli("resolver", "add", "Quad9", "--kind", "dns") == Result(2, "", "subnet-tracker: error: endpoint: dns resolvers need an endpoint\n")
    assert cli("resolver", "add", "local", "--kind", "system", "--endpoint", "udp://1.1.1.1") == Result(2, "", "subnet-tracker: error: endpoint: system resolvers have no endpoint\n")
    assert cli("resolver", "add", "Quad9", "--kind", "dns", "--endpoint", "udp://9.9.9.9", "--max-qps", "0") == Result(2, "", "subnet-tracker: error: max_qps: must be 1 to 10000\n")
    assert (count(db, "resolvers"), trigger_lines(tmp_path)) == (0, [])


def test_edit_without_options_or_of_an_unknown_resolver_writes_nothing(cli: Cli, db: sqlite3.Connection) -> None:
    cli("resolver", "add", "local", "--kind", "system")
    assert cli("resolver", "edit", "local") == Result(2, "", "subnet-tracker: error: nothing to change, give at least one option\n")
    assert cli("resolver", "rm", "missing") == Result(2, "", "subnet-tracker: error: name: 'missing' does not exist\n")
    assert count(db, "resolvers") == 1
