import sqlite3
from pathlib import Path

from subnet_tracker.types import Timestamp

TRIGGER_FILE = Path("/run/subnet-tracker/trigger")


def trigger_line(now: Timestamp, actor: str) -> str:
    return " ".join([str(now), *actor.split()]) + "\n"


def notify_tick(conn: sqlite3.Connection, now: Timestamp, actor: str, path: Path = TRIGGER_FILE) -> OSError | None:
    if conn.in_transaction:
        raise RuntimeError("the trigger file is written after commit, or tick may read requests before they are committed")
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(trigger_line(now, actor))
    except OSError as e:
        return e
    return None
