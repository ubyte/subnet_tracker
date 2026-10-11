import json
import sqlite3
from ipaddress import ip_address
from pathlib import Path

import pytest

from cli_world import ACTOR, NOW, Cli, Result, answering, trigger_lines
from domains_world import count
from groups_world import domain, group_names, make_group
from scan_fakes import FakeDns, FakeExchange
from subnet_tracker.cli import commands
from subnet_tracker.cli.output import table
from subnet_tracker.core.domains import find_domain
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.requests import TickWork, claim_requests, finish_requests
from subnet_tracker.core.settings import DiscoverySettings
from subnet_tracker.discovery.fetch import HttpResponse
from subnet_tracker.types import DomainName, DomainSource, ReconnectMode

URL = "https://www.example.com/some/page"
PAGE = b"""\
<a href="https://static.example.com/a.js">s</a>
<a href="https://fonts.gstatic.com/a.woff">f</a>
<a href="https://www.w3.org/TR">w</a>
"""
ROWS = (
    ("yes", "www.example.com", "same", "new", "page host"),
    ("yes", "static.example.com", "same", "new", "a href"),
    ("no", "fonts.gstatic.com", "third", "new", "a href"),
)
HEAD = ("add", "name", "role", "state", "sources")


def response(body: bytes, status: int = 200, headers: tuple[tuple[str, str], ...] = ()) -> HttpResponse:
    return HttpResponse(status, headers, body)


def example_fetch() -> tuple[FakeDns, FakeExchange]:
    return FakeDns({"www.example.com": ("1.2.3.4",)}), FakeExchange({URL: response(PAGE)})


def report(rows: tuple[tuple[str, str, str, str, str], ...] = ROWS, *extra: str) -> str:
    return "\n".join((table(HEAD, rows), "ignored: w3.org", *extra))


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> None:
        raise AssertionError("scan tried to use the network")

    monkeypatch.setattr(commands.scan, "page_fetch", refuse)


def scanning(cli: Cli, monkeypatch: pytest.MonkeyPatch, dns: FakeDns, exchange: FakeExchange) -> Cli:
    monkeypatch.setattr(commands.scan, "page_fetch", lambda: (dns, exchange))
    return cli


def test_scan_adds_the_same_site_into_a_new_group(cli: Cli, db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dns, exchange = example_fetch()
    result = scanning(cli, monkeypatch, dns, exchange)("scan", URL, "--yes")
    added = "added 2 in batch 1: www.example.com, static.example.com"
    assert result == Result(0, report(ROWS, "new group example.com", added) + "\n", "")
    sent = exchange.sent[0]
    discovery = DiscoverySettings()
    assert (sent.url, sent.pin, sent.timeout, sent.max_bytes, sent.user_agent) == (URL, ip_address("1.2.3.4"), float(discovery.timeout), discovery.max_bytes, discovery.user_agent)
    assert db.execute("SELECT kind, actor, input, added FROM add_batches").fetchone()[:] == ("scan", ACTOR, URL, 2)
    found = domain(db, "example.com")
    assert (found.canonical.name, found.pair.name, found.source) == ("www.example.com", "example.com", DomainSource.DISCOVERED)
    assert group_names(db, found.id) == {"example.com"}
    assert group_names(db, domain(db, "static.example.com").id) == {"example.com"}
    assert find_domain(db, DomainName("fonts.gstatic.com")) is None
    events = list(reversed(list_events(db, EventFilter(10))))
    assert [(event.kind, event.actor) for event in events] == [(EventKind.GROUP_ADDED, ACTOR), (EventKind.DOMAIN_ADDED, ACTOR), (EventKind.DOMAIN_ADDED, ACTOR)]
    assert events[0].details["name"] == "example.com"
    claim = claim_requests(db)
    assert claim is not None
    assert claim.work == TickWork(new_domains=frozenset({found.id, domain(db, "static.example.com").id}), apply=ReconnectMode.AUTO)
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]


@pytest.mark.parametrize(
    ("answer", "code", "error"),
    [(False, 1, "not confirmed, nothing added"), (None, 2, "stdin is not a terminal to confirm on, pass --yes to add without asking")],
)
def test_refused_confirmation_writes_nothing(cli: Cli, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, answer: bool | None, code: int, error: str) -> None:
    prompts: list[str] = []
    dns, exchange = example_fetch()
    result = answering(scanning(cli, monkeypatch, dns, exchange), answer, prompts)("scan", URL)
    assert result == Result(code, "", f"subnet-tracker: error: {error}\n")
    assert prompts == [report() + "\nadd 2 domains into new group example.com?"]
    assert exchange.sent
    assert (count(db, "add_batches"), count(db, "groups"), count(db, "domains"), count(db, "requests")) == (0, 0, 0, 0)


def test_answering_yes_adds_and_yes_flag_does_not_ask(cli: Cli, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []
    dns, exchange = example_fetch()
    accepted = answering(scanning(cli, monkeypatch, dns, exchange), True, prompts)("scan", URL)
    assert accepted.code == 0
    assert prompts == [report() + "\nadd 2 domains into new group example.com?"]
    assert count(db, "domains") == 2
    prompts.clear()
    again = answering(scanning(cli, monkeypatch, dns, exchange), True, prompts)("scan", URL, "--yes")
    exists = (
        ("no", "www.example.com", "same", "exists", "page host"),
        ("no", "static.example.com", "same", "exists", "a href"),
        ("no", "fonts.gstatic.com", "third", "new", "a href"),
    )
    assert prompts == []
    assert again == Result(0, report(exists, "new group example.com (2) not created", "added 0 in batch 2") + "\n", "")
    assert count(db, "groups") == 1


def test_include_third_party_adds_those_domains_too(cli: Cli, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    dns, exchange = example_fetch()
    result = scanning(cli, monkeypatch, dns, exchange)("scan", URL, "--include-third-party", "--yes")
    assert result.code == 0
    assert "added 3 in batch 1: www.example.com, static.example.com, fonts.gstatic.com" in result.out
    assert group_names(db, domain(db, "fonts.gstatic.com").id) == {"example.com"}
    assert group_names(db, domain(db, "www.example.com").id) == {"example.com"}


def test_group_flags_select_none_existing_or_a_new_name(cli: Cli, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    make_group(db, "Work")
    dns = FakeDns({"www.example.com": ("1.2.3.4",), "other.org": ("8.8.8.8",), "third.test": ("9.9.9.9",)})
    pages = {URL: response(PAGE), "https://other.org/": response(b""), "https://third.test/": response(b"")}
    exchange = FakeExchange(pages)
    run = scanning(cli, monkeypatch, dns, exchange)
    ungrouped = run("scan", URL, "--no-group", "--yes")
    assert ungrouped.code == 0
    assert "no group\nadded 2 in batch 1: www.example.com, static.example.com\n" in ungrouped.out
    assert group_names(db, domain(db, "www.example.com").id) == set()
    grouped = run("scan", "https://other.org/", "--group", "Work", "--yes")
    assert "group Work\nadded 1 in batch 2: other.org\n" in grouped.out
    assert group_names(db, domain(db, "other.org").id) == {"Work"}
    named = run("scan", "https://third.test/", "--new-group", "Custom", "--yes")
    assert named.code == 0
    assert "new group Custom\nadded 1 in batch 3: third.test\n" in named.out
    assert group_names(db, domain(db, "third.test").id) == {"Custom"}
    assert {name for (name,) in db.execute("SELECT name FROM groups")} == {"Work", "Custom"}


def test_rejected_group_arguments_do_not_fetch(cli: Cli, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    make_group(db, "Work")
    dns, exchange = example_fetch()
    run = scanning(cli, monkeypatch, dns, exchange)
    assert run("scan", URL, "--group", "Missing") == Result(2, "", "subnet-tracker: error: name: 'Missing' does not exist\n")
    assert run("scan", URL, "--new-group", "Work") == Result(2, "", "subnet-tracker: error: name: group 'Work' already exists\n")
    assert run("scan", URL, "--new-group", "None") == Result(2, "", "subnet-tracker: error: name: 'None' is reserved for domains without a group\n")
    assert "line breaks" in run("scan", URL, "--new-group", "bad\nname").err
    assert run("scan", URL, "--group", "Work", "--no-group").code == 2
    assert run("scan").code == 2
    assert exchange.sent == []
    assert (count(db, "add_batches"), count(db, "domains")) == (0, 0)


def test_a_taken_default_name_gains_a_suffix_and_extra_sites_add_a_plus(cli: Cli, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    make_group(db, "example.com")
    dns, exchange = example_fetch()
    suffixed = scanning(cli, monkeypatch, dns, exchange)("scan", URL, "--yes")
    assert suffixed.code == 0
    assert "new group example.com (2)\n" in suffixed.out
    assert group_names(db, domain(db, "www.example.com").id) == {"example.com (2)"}
    both = FakeDns({"www.example.com": ("1.2.3.4",), "other.org": ("8.8.8.8",)})
    pages = FakeExchange({URL: response(b""), "https://other.org/x": response(b"")})
    paired = scanning(cli, monkeypatch, both, pages)("scan", URL, "https://other.org/x", "--yes")
    assert paired.code == 0
    assert "new group example.com +1\n" in paired.out
    assert group_names(db, domain(db, "other.org").id) == {"example.com +1"}


def test_tracked_domains_stay_put_and_a_repeat_does_not_notify(cli: Cli, db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli("add", "example.com")
    claim = claim_requests(db)
    assert claim is not None
    finish_requests(db, claim)
    dns, exchange = example_fetch()
    result = scanning(cli, monkeypatch, dns, exchange)("scan", URL, "--yes")
    assert result.code == 0
    assert "added 1 in batch 2: static.example.com" in result.out
    assert group_names(db, domain(db, "example.com").id) == set()
    assert group_names(db, domain(db, "static.example.com").id) == {"example.com"}
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}", f"{NOW} {ACTOR}"]
    claim = claim_requests(db)
    assert claim is not None
    assert claim.work.new_domains == frozenset({domain(db, "static.example.com").id})
    finish_requests(db, claim)
    again = scanning(cli, monkeypatch, dns, exchange)("scan", URL, "--yes")
    assert again.code == 0
    assert "added 0 in batch 3" in again.out
    assert count(db, "groups") == 1
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}", f"{NOW} {ACTOR}"]
    assert claim_requests(db) is None


def test_page_errors_are_partial_when_another_page_is_added(cli: Cli, db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ok, bad = "https://ok.example/", "https://broken.example/"
    dns = FakeDns({"ok.example": ("1.2.3.4",), "broken.example": ("8.8.8.8",)})
    exchange = FakeExchange({ok: response(b""), bad: TimeoutError()})
    result = scanning(cli, monkeypatch, dns, exchange)("scan", bad, ok, "--yes")
    assert result.code == 3
    assert result.err == ""
    assert "error: https://broken.example/: timeout after 10s" in result.out
    assert "added 1 in batch 1: ok.example" in result.out
    assert "new group broken.example +1" in result.out
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]
    evil, good = "https://evil.example/", "https://safe.example/"
    private = FakeDns({"evil.example": ("10.1.2.3",), "safe.example": ("1.2.3.4",)})
    pages = FakeExchange({good: response(b"<a href='https://static.safe.example/'></a>")})
    blocked = scanning(cli, monkeypatch, private, pages)("scan", evil, good, "--yes")
    assert blocked.code == 3
    assert "error: https://evil.example/: private address 10.1.2.3" in blocked.out
    assert "added 2 in batch 2: safe.example, static.safe.example" in blocked.out
    assert [item.url for item in pages.sent] == [good]


def test_every_failed_page_exits_1_and_records_no_group(cli: Cli, db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = "https://broken.example/"
    dns = FakeDns({"broken.example": ("8.8.8.8",)})
    exchange = FakeExchange({bad: TimeoutError()})
    result = scanning(cli, monkeypatch, dns, exchange)("scan", bad, "--yes")
    assert result == Result(
        1,
        "no domains\nerror: https://broken.example/: timeout after 10s\nnew group broken.example not created\nadded 0 in batch 1\n",
        "",
    )
    assert (count(db, "groups"), count(db, "domains"), count(db, "requests")) == (0, 0, 0)
    assert db.execute("SELECT kind, added FROM add_batches").fetchone()[:] == ("scan", 0)
    assert trigger_lines(tmp_path) == []


def test_more_than_20_urls_is_a_usage_error(cli: Cli, db: sqlite3.Connection) -> None:
    urls = [f"https://s{i}.example.com/" for i in range(21)]
    assert cli("scan", *urls, "--yes") == Result(2, "", "subnet-tracker: error: at most 20 URLs\n")
    assert count(db, "add_batches") == 0


def test_json_reports_the_selection_the_group_and_the_batch(cli: Cli, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    dns, exchange = example_fetch()
    result = scanning(cli, monkeypatch, dns, exchange)("scan", URL, "--yes", "--json")
    assert (result.code, result.err) == (0, "")
    assert json.loads(result.out) == {
        "candidates": [
            {"name": "www.example.com", "role": "same", "exists": False, "add": True, "sources": ["page host"]},
            {"name": "static.example.com", "role": "same", "exists": False, "add": True, "sources": ["a href"]},
            {"name": "fonts.gstatic.com", "role": "third", "exists": False, "add": False, "sources": ["a href"]},
        ],
        "ignored": ["w3.org"],
        "errors": [],
        "group": {"name": "example.com", "created": True},
        "batch_id": 1,
        "added": ["www.example.com", "static.example.com"],
        "duplicates": [],
        "invalid": [],
        "networks": [],
        "wildcards": [],
    }


def test_discovery_settings_reach_the_fetch(cli: Cli, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli("settings", "set", "discovery.timeout", "4s").code == 0
    assert cli("settings", "set", "discovery.ignore", "fonts.gstatic.com").code == 0
    dns, exchange = example_fetch()
    result = scanning(cli, monkeypatch, dns, exchange)("scan", URL, "--yes")
    assert result.code == 0
    assert exchange.sent[0].timeout == 4
    assert "ignored: fonts.gstatic.com" in result.out
    assert "www.w3.org" in result.out
    assert find_domain(db, DomainName("fonts.gstatic.com")) is None
    assert find_domain(db, DomainName("www.w3.org")) is None


def test_urls_without_a_registrable_domain_need_an_explicit_group(cli: Cli, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://co.uk/"
    dns = FakeDns({"co.uk": ("1.2.3.4",)})
    exchange = FakeExchange({url: response(b"")})
    run = scanning(cli, monkeypatch, dns, exchange)
    refused = run("scan", url, "--yes")
    assert refused == Result(2, "", "subnet-tracker: error: no group name for these URLs, pass --new-group NAME or --no-group\n")
    assert exchange.sent
    assert count(db, "add_batches") == 0
    added = run("scan", url, "--no-group", "--yes")
    assert added.code == 0
    assert "no group\n" in added.out
    assert domain(db, "co.uk").source == DomainSource.DISCOVERED
    assert count(db, "groups") == 0
