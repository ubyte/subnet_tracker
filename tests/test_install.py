import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from db_rows import tables
from domains_world import NOW, count
from subnet_tracker.core import resolvers
from subnet_tracker.core.db import connect, load_migrations
from subnet_tracker.core.install import migrate_database
from subnet_tracker.core.resolvers import DEFAULT_RESOLVERS, NewResolver, delete_resolver, list_resolvers
from subnet_tracker.types import ResolverKind


@pytest.fixture
def empty(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(tmp_path / "state.db")) as conn:
        yield conn


def test_migrating_an_empty_database_creates_the_default_resolvers(empty: sqlite3.Connection) -> None:
    assert migrate_database(empty, NOW) == tuple(m.version for m in load_migrations())
    assert [r.name for r in list_resolvers(empty)] == [r.name for r in DEFAULT_RESOLVERS]
    assert not empty.in_transaction


def test_default_resolvers_are_never_created_again(empty: sqlite3.Connection) -> None:
    migrate_database(empty, NOW)
    for resolver in list_resolvers(empty):
        delete_resolver(empty, resolver.id)
    assert migrate_database(empty, NOW) == ()
    assert count(empty, "resolvers") == 0


def test_a_migrated_database_gets_no_default_resolvers(db: sqlite3.Connection) -> None:
    assert migrate_database(db, NOW) == ()
    assert count(db, "resolvers") == 0


def test_failing_defaults_roll_the_schema_back(empty: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolvers, "DEFAULT_RESOLVERS", (NewResolver("", ResolverKind.SYSTEM),))
    with pytest.raises(ValueError, match="default resolver"):
        migrate_database(empty, NOW)
    assert (empty.execute("PRAGMA user_version").fetchone()[0], tables(empty)) == (0, set())
