import sqlite3
from pathlib import Path

import pytest

from domains_world import NOW
from subnet_tracker.core.db import transaction
from subnet_tracker.core.trigger import TRIGGER_FILE, notify_tick, trigger_line
from subnet_tracker.types import Timestamp


def test_trigger_file_is_the_one_tick_path_watches() -> None:
    assert Path("/run/subnet-tracker/trigger") == TRIGGER_FILE


@pytest.mark.parametrize(
    ("actor", "line"),
    [
        ("cli:alice", f"{NOW} cli:alice\n"),
        ("web:10.8.0.2/laptop", f"{NOW} web:10.8.0.2/laptop\n"),
        ("agent:office\npc", f"{NOW} agent:office pc\n"),
    ],
)
def test_trigger_line_is_one_line(actor: str, line: str) -> None:
    assert trigger_line(NOW, actor) == line


def test_notify_appends_a_line(db: sqlite3.Connection, tmp_path: Path) -> None:
    trigger = tmp_path / "trigger"
    assert notify_tick(db, NOW, "cli:alice", trigger) is None
    assert notify_tick(db, Timestamp(NOW + 1), "tick", trigger) is None
    assert trigger.read_text() == f"{NOW} cli:alice\n{NOW + 1} tick\n"


def test_missing_directory_is_reported_not_raised(db: sqlite3.Connection, tmp_path: Path) -> None:
    error = notify_tick(db, NOW, "cli:alice", tmp_path / "absent" / "trigger")
    assert isinstance(error, FileNotFoundError)


def test_notify_refuses_inside_a_transaction(db: sqlite3.Connection, tmp_path: Path) -> None:
    trigger = tmp_path / "trigger"
    with pytest.raises(RuntimeError, match="after commit"), transaction(db):
        notify_tick(db, NOW, "cli:alice", trigger)
    assert not trigger.exists()
