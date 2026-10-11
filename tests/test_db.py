import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from db_rows import tables
from subnet_tracker.core.db import Migration, MigrationError, apply_migrations, connect, load_migrations, migrate, parse_migrations, transaction

EXTRA = Migration(2, "extra", "CREATE TABLE extra (id INTEGER PRIMARY KEY) STRICT;")


def user_version(conn: sqlite3.Connection) -> int:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


def schema(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(row["name"], row["sql"]) for row in conn.execute("SELECT name, sql FROM sqlite_schema ORDER BY name")]


def test_connect_sets_pragmas(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "state.db")) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_connect_returns_rows_by_column_name(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "state.db")) as conn:
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1


def test_migrate_empty_db_applies_all_and_sets_user_version(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "state.db")) as conn:
        assert migrate(conn) == (1,)
        assert user_version(conn) == 1
        assert "domains" in tables(conn)


def test_migrate_is_idempotent(db: sqlite3.Connection) -> None:
    before = schema(db)
    assert migrate(db) == ()
    assert user_version(db) == 1
    assert schema(db) == before


def test_migrations_apply_to_db_of_previous_version(db: sqlite3.Connection) -> None:
    assert apply_migrations(db, (*load_migrations(), EXTRA)) == (2,)
    assert user_version(db) == 2
    assert "extra" in tables(db)


def test_failed_migration_leaves_db_unchanged(db: sqlite3.Connection) -> None:
    broken = Migration(2, "broken", "CREATE TABLE extra (id INTEGER PRIMARY KEY) STRICT; INSERT INTO missing VALUES (1);")
    with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
        apply_migrations(db, (*load_migrations(), broken))
    assert user_version(db) == 1
    assert "extra" not in tables(db)
    assert not db.in_transaction


def test_db_newer_than_known_migrations_is_rejected(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA user_version = 2")
    with pytest.raises(MigrationError, match="newer"):
        migrate(db)


def test_load_migrations_starts_with_init() -> None:
    assert load_migrations()[0].version == 1
    assert load_migrations()[0].name == "init"


def test_init_migration_matches_design_data_model() -> None:
    design = (Path(__file__).parents[1] / "docs" / "design.md").read_text(encoding="utf-8")
    section = design[design.index("\n## 5. ") : design.index("\n## 6. ")]
    assert "".join(re.findall(r"^```sql\n(.*?)^```$", section, re.DOTALL | re.MULTILINE)) == load_migrations()[0].sql


def test_parse_migrations_orders_by_number() -> None:
    assert parse_migrations({"0002_b.sql": "B", "0001_a.sql": "A"}) == (Migration(1, "a", "A"), Migration(2, "b", "B"))


@pytest.mark.parametrize("files", [{"init.sql": ""}, {"0001_a.txt": ""}, {"1_a.sql": ""}, {"0001_a.sql": "", "0003_c.sql": ""}, {"0002_b.sql": ""}, {"0001_a.sql": "", "0001_b.sql": ""}])
def test_parse_migrations_rejects_bad_names_and_gaps(files: dict[str, str]) -> None:
    with pytest.raises(MigrationError):
        parse_migrations(files)


def test_transaction_commits(db: sqlite3.Connection) -> None:
    with transaction(db):
        db.execute("INSERT INTO tags (name) VALUES ('kept')")
    assert [row["name"] for row in db.execute("SELECT name FROM tags")] == ["kept"]


def insert_tag_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        db.execute("INSERT INTO tags (name) VALUES ('lost')")
        raise RuntimeError


def test_transaction_rolls_back_on_error(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        insert_tag_and_fail(db)
    assert db.execute("SELECT count(*) FROM tags").fetchone()[0] == 0
    assert not db.in_transaction
