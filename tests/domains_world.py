import sqlite3
from collections.abc import Callable

from subnet_tracker.core.batches import AddResult, add_text
from subnet_tracker.core.db import transaction
from subnet_tracker.types import BatchKind, DomainName, Timestamp

NOW = Timestamp(1_760_000_000)
ACTOR = "web:10.8.0.2"


def dn(*names: str) -> tuple[DomainName, ...]:
    return tuple(DomainName(n) for n in names)


def paste(db: sqlite3.Connection, text: str, now: Timestamp = NOW) -> AddResult:
    return add_text(db, BatchKind.PASTE, ACTOR, text, now)


def paste_and_fail(db: sqlite3.Connection, text: str, then: Callable[[], object]) -> None:
    with transaction(db):
        paste(db, text)
        then()
        raise RuntimeError


def count(db: sqlite3.Connection, table: str) -> int:
    return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
