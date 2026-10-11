import sqlite3

import pytest

from db_rows import ROWS, Row, insert, tables

CASCADE, SET_NULL = "CASCADE", "SET NULL"

FOREIGN_KEYS = [
    ("domains", "batch_id", "add_batches", SET_NULL),
    ("names", "domain_id", "domains", CASCADE),
    ("domain_groups", "domain_id", "domains", CASCADE),
    ("domain_groups", "group_id", "groups", CASCADE),
    ("domain_tags", "domain_id", "domains", CASCADE),
    ("domain_tags", "tag_id", "tags", CASCADE),
    ("agents", "resolver_id", "resolvers", CASCADE),
    ("name_resolves", "name_id", "names", CASCADE),
    ("name_resolves", "resolver_id", "resolvers", CASCADE),
    ("resolve_leases", "name_id", "names", CASCADE),
    ("resolve_leases", "resolver_id", "resolvers", CASCADE),
    ("addresses", "name_id", "names", CASCADE),
    ("sightings", "address_id", "addresses", CASCADE),
    ("sightings", "resolver_id", "resolvers", CASCADE),
    ("route_groups", "route_id", "global_routes", CASCADE),
    ("route_groups", "group_id", "groups", CASCADE),
    ("plan_routes", "global_route_id", "global_routes", SET_NULL),
    ("plan_route_parts", "route_id", "plan_routes", CASCADE),
    ("resolve_runs", "resolver_id", "resolvers", CASCADE),
    ("resolve_run_resolvers", "run_id", "resolve_runs", CASCADE),
    ("resolve_run_resolvers", "resolver_id", "resolvers", CASCADE),
]

# Makes a ROWS row valid once its foreign key column points at parent 1.
REFERENCING: dict[str, Row] = {"names": {"name": "www.example.com", "canonical": 0}, "plan_routes": {"kind": "global"}, "resolve_runs": {"trigger": "agent"}}


def test_foreign_keys_and_on_delete_actions(db: sqlite3.Connection) -> None:
    actual = [(table, fk["from"], fk["table"], fk["on_delete"]) for table in sorted(tables(db)) for fk in db.execute(f"PRAGMA foreign_key_list({table})")]
    assert sorted(actual) == sorted(FOREIGN_KEYS)


@pytest.mark.parametrize(("table", "column", "parent", "action"), FOREIGN_KEYS, ids=[f"{t}.{c}" for t, c, _, _ in FOREIGN_KEYS])
def test_deleting_parent_applies_on_delete(seeded_db: sqlite3.Connection, table: str, column: str, parent: str, action: str) -> None:
    rowid = insert(seeded_db, table, {**ROWS[table], **REFERENCING.get(table, {}), column: 1})
    seeded_db.execute(f"DELETE FROM {parent} WHERE id = 1")
    row = seeded_db.execute(f"SELECT {column} FROM {table} WHERE rowid = ?", (rowid,)).fetchone()
    if action == CASCADE:
        assert row is None
    else:
        assert row[column] is None


def test_unknown_parent_is_rejected(seeded_db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        insert(seeded_db, "names", {**ROWS["names"], "domain_id": 99})


def test_deleting_domain_removes_both_names_with_their_addresses(seeded_db: sqlite3.Connection) -> None:
    pair_id = insert(seeded_db, "names", {"domain_id": 1, "name": "www.example.com", "canonical": 0})
    address_id = insert(seeded_db, "addresses", {"name_id": pair_id, "ip": "192.0.2.1", "family": 4, "ip_bin": bytes([192, 0, 2, 1])})
    insert(seeded_db, "sightings", {**ROWS["sightings"], "address_id": address_id})
    seeded_db.execute("DELETE FROM domains WHERE id = 1")
    assert [seeded_db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("names", "addresses", "sightings")] == [0, 0, 0]
