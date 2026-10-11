import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from operator import attrgetter
from pathlib import Path

# busy_timeout goes first: switching to WAL may wait for a lock held by another process.
_PRAGMAS = "PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL;"
_MIGRATION_FILE = re.compile(r"([0-9]{4})_(\w+)\.sql")


class MigrationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.executescript(_PRAGMAS)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def parse_migrations(sources: Mapping[str, str]) -> tuple[Migration, ...]:
    migrations = []
    for file_name, sql in sources.items():
        if not (m := _MIGRATION_FILE.fullmatch(file_name)):
            raise MigrationError(f"invalid migration file name {file_name!r}, expected NNNN_name.sql")
        migrations.append(Migration(int(m[1]), m[2], sql))
    migrations.sort(key=attrgetter("version"))
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise MigrationError(f"migration numbers must run from 0001 without gaps, got {', '.join(sorted(sources))}")
    return tuple(migrations)


def pending_migrations(migrations: Sequence[Migration], version: int) -> tuple[Migration, ...]:
    latest = migrations[-1].version if migrations else 0
    if version > latest:
        raise MigrationError(f"database schema version {version} is newer than the latest known migration {latest}")
    return tuple(m for m in migrations if m.version > version)


@cache
def load_migrations() -> tuple[Migration, ...]:
    root = files("subnet_tracker.core").joinpath("migrations")
    return parse_migrations({f.name: f.read_text(encoding="utf-8") for f in root.iterdir() if f.name.endswith(".sql")})


def apply_migrations(conn: sqlite3.Connection, migrations: Sequence[Migration]) -> tuple[int, ...]:
    with transaction(conn):
        todo = pending_migrations(migrations, conn.execute("PRAGMA user_version").fetchone()[0])
        for m in todo:
            conn.executescript(m.sql)
            conn.execute(f"PRAGMA user_version = {m.version}")
    return tuple(m.version for m in todo)


def migrate(conn: sqlite3.Connection) -> tuple[int, ...]:
    return apply_migrations(conn, load_migrations())
