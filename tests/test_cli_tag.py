import sqlite3
from pathlib import Path

from cli_world import Cli, Result, trigger_lines
from domains_world import NOW, count, paste
from groups_world import domain
from subnet_tracker.cli.output import table
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.memberships import SetChange
from subnet_tracker.core.requests import claim_requests
from subnet_tracker.core.tags import edit_domain_tags, ensure_tags, find_tag
from subnet_tracker.types import FieldError


def test_rename_merge_and_delete(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert cli("tag", "list") == Result(0, table(("name", "domains"), []) + "\n", "")
    paste(db, "a.com b.com")
    ids = ensure_tags(db, ["video", "films"])
    assert not isinstance(ids, FieldError)
    video, films = find_tag(db, "video"), find_tag(db, "films")
    assert video is not None
    assert films is not None
    edit_domain_tags(db, [domain(db, "a.com").id], SetChange(add=frozenset({video.id})), NOW)
    edit_domain_tags(db, [domain(db, "a.com").id, domain(db, "b.com").id], SetChange(add=frozenset({films.id})), NOW)
    assert cli("tag", "list") == Result(0, table(("name", "domains"), [("films", "2"), ("video", "1")]) + "\n", "")
    assert cli("tag", "rename", "video", "video") == Result(0, "nothing changed\n", "")
    assert trigger_lines(tmp_path) == []
    assert cli("tag", "rename", " video ", " films ") == Result(0, "merged tag video into films\n", "")
    assert find_tag(db, "video") is None
    assert domain(db, "a.com").updated_at == NOW
    assert claim_requests(db) is None
    assert trigger_lines(tmp_path) == []
    assert [e.kind for e in list_events(db, EventFilter(1))] == [EventKind.TAG_MERGED]
    assert cli("tag", "rm", "films") == Result(0, "deleted tag films\n", "")
    assert count(db, "tags") == 0
    assert claim_requests(db) is None


def test_unknown_and_invalid_names_are_usage_errors(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    ensure_tags(db, ["video"])
    assert cli("tag", "rename", "missing", "other") == Result(2, "", "subnet-tracker: error: name: 'missing' does not exist\n")
    assert cli("tag", "rename", "video", "a\nb") == Result(2, "", "subnet-tracker: error: name: line breaks are not allowed\n")
    assert cli("tag", "rm", "missing") == Result(2, "", "subnet-tracker: error: name: 'missing' does not exist\n")
    assert find_tag(db, "video") is not None
    assert trigger_lines(tmp_path) == []
