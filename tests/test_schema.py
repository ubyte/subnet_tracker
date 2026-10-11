import sqlite3

import pytest
from _pytest.mark import ParameterSet

from db_rows import ROWS, insert, tables


def case(table: str, label: str, **override: object) -> ParameterSet:
    return pytest.param(table, override, id=f"{table}.{label}")


REJECTED = [
    case("groups", "name-empty", name=""),
    case("groups", "name-too-long", name="g" * 101),
    case("groups", "prefix_v4-low", prefix_v4=7),
    case("groups", "prefix_v4-high", prefix_v4=33),
    case("groups", "prefix_v6-low", prefix_v6=15),
    case("groups", "prefix_v6-high", prefix_v6=129),
    case("groups", "enabled", enabled=2),
    case("add_batches", "kind", kind="email"),
    case("add_batches", "invalid-json", invalid="[x"),
    case("domains", "prefix_v4-low", prefix_v4=7),
    case("domains", "prefix_v4-high", prefix_v4=33),
    case("domains", "prefix_v6-low", prefix_v6=15),
    case("domains", "prefix_v6-high", prefix_v6=129),
    case("domains", "enabled", enabled=-1),
    case("domains", "source", source="cli"),
    case("names", "name-empty", name=""),
    case("names", "name-too-long", name="a" * 251 + ".io"),
    case("names", "name-upper", name="Example.org"),
    case("names", "canonical", canonical=2),
    case("tags", "name-empty", name=""),
    case("tags", "name-too-long", name="t" * 51),
    case("resolvers", "name-empty", name=""),
    case("resolvers", "name-too-long", name="r" * 101),
    case("resolvers", "kind", kind="dot"),
    case("resolvers", "use_for_routes", use_for_routes=2),
    case("resolvers", "max_qps-zero", max_qps=0),
    case("resolvers", "system-with-endpoint", kind="system", endpoint="udp://1.1.1.1"),
    case("resolvers", "agent-with-endpoint", kind="agent", endpoint="udp://1.1.1.1"),
    case("resolvers", "dns-without-endpoint", kind="dns"),
    case("resolvers", "doh-without-endpoint", kind="doh"),
    case("resolvers", "browser-without-endpoint", kind="browser"),
    case("agents", "cert_sha256-short", cert_sha256="0" * 63),
    case("agents", "mode", mode="daemon"),
    case("agents", "dns_servers-json", dns_servers="1.1.1.1"),
    case("name_resolves", "status", status="timeout"),
    case("name_resolves", "cnames-json", cnames="a.example.com"),
    case("addresses", "family", family=5),
    case("addresses", "v6-with-4-bytes", ip_bin=bytes(4)),
    case("addresses", "v4-with-16-bytes", ip="192.0.2.2", family=4, ip_bin=bytes(16)),
    case("sightings", "last-before-first", first_seen_at=10, last_seen_at=9),
    case("global_routes", "family", family=0),
    case("global_routes", "enabled", enabled=2),
    case("plan_state", "id", id=2),
    case("plan_state", "plan_sha256-short", plan_sha256="0" * 63),
    case("plan_state", "warnings-json", warnings="[1,"),
    case("plan_routes", "family", family=46),
    case("plan_routes", "kind", kind="manual"),
    case("plan_routes", "reconnected", reconnected=2),
    case("plan_routes", "dns-with-global-route", global_route_id=1),
    case("plan_routes", "aggregated-with-global-route", kind="aggregated", global_route_id=1),
    case("settings", "value-json", value="14d"),
    case("scheduler_state", "id", id=2),
    case("reconnect_state", "id", id=2),
    case("reconnect_state", "pending_mode", pending_mode="now", pending_since=0),
    case("reconnect_state", "mode-without-since", pending_mode="auto"),
    case("reconnect_state", "since-without-mode", pending_since=0),
    case("web_devices", "token_sha256-long", token_sha256="0" * 65),
    case("requests", "kind", kind="reconnect"),
    case("requests", "payload-json", payload=""),
    case("resolve_runs", "trigger", trigger="cron"),
    case("resolve_runs", "browser-without-resolver", trigger="browser"),
    case("resolve_runs", "agent-without-resolver", trigger="agent"),
    case("resolve_runs", "timer-with-resolver", trigger="timer", resolver_id=1),
    case("resolve_run_resolvers", "healthy", healthy=2),
    case("apply_runs", "trigger", trigger="timer"),
    case("apply_runs", "diff-json", diff="+1.2.3.0/24"),
    case("apply_runs", "file_sha256-short", file_sha256=""),
    case("apply_runs", "file_changed", file_changed=2),
    case("apply_runs", "reconnect", reconnect="later"),
    case("apply_runs", "status", status="failed"),
    case("events", "details-json", details="{"),
]

ACCEPTED = [
    case("groups", "bounds-low", name="g", prefix_v4=8, prefix_v6=16),
    case("groups", "bounds-high", name="g" * 100, prefix_v4=32, prefix_v6=128, enabled=0),
    case("domains", "bounds-low", prefix_v4=8, prefix_v6=16, enabled=0),
    case("domains", "bounds-high", prefix_v4=32, prefix_v6=128),
    case("names", "name-max-length", name="a" * 250 + ".io"),
    case("tags", "name-max-length", name="t" * 50),
    case("resolvers", "agent-without-endpoint", kind="agent"),
    case("resolvers", "dns-with-endpoint", kind="dns", endpoint="tls://1.1.1.1"),
    case("resolvers", "doh-with-endpoint", kind="doh", endpoint="https://cloudflare-dns.com/dns-query"),
    case("resolvers", "browser-with-endpoint", kind="browser", endpoint="https://dns.google/resolve"),
    case("agents", "mode-and-dns-servers", mode="loop", dns_servers='["10.0.0.1"]'),
    case("addresses", "v4-with-4-bytes", ip="192.0.2.2", family=4, ip_bin=bytes(4)),
    case("sightings", "last-after-first", first_seen_at=9, last_seen_at=10),
    case("plan_routes", "global-with-route", kind="global", global_route_id=1),
    case("plan_routes", "global-without-route", kind="global"),
    case("reconnect_state", "pending", pending_mode="on_leave", pending_since=0),
    case("resolve_runs", "agent-with-resolver", trigger="agent", resolver_id=1),
    case("resolve_runs", "browser-with-resolver", trigger="browser", resolver_id=1),
]

WRONG_TYPE: dict[str, object] = {"INTEGER": "x", "REAL": "x", "TEXT": b"x", "BLOB": 1}


def test_rows_cover_every_table(db: sqlite3.Connection) -> None:
    assert tables(db) == set(ROWS)


def test_named_indexes_and_triggers(db: sqlite3.Connection) -> None:
    objects = {(row["type"], row["name"]) for row in db.execute("SELECT type, name FROM sqlite_schema WHERE type IN ('index', 'trigger') AND name NOT LIKE 'sqlite_%'")}
    indexes = {"domains_batch", "names_canonical", "names_pair", "names_last_attempt", "names_last_success", "name_resolves_due", "addresses_range"}
    assert objects == {("index", name) for name in indexes} | {("trigger", "names_pair_shape")}


@pytest.mark.parametrize("table", sorted(ROWS))
def test_valid_row_is_accepted(seeded_db: sqlite3.Connection, table: str) -> None:
    insert(seeded_db, table, ROWS[table])


@pytest.mark.parametrize(("table", "override"), REJECTED)
def test_check_rejects(seeded_db: sqlite3.Connection, table: str, override: dict[str, object]) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        insert(seeded_db, table, {**ROWS[table], **override})


@pytest.mark.parametrize(("table", "override"), ACCEPTED)
def test_check_accepts(seeded_db: sqlite3.Connection, table: str, override: dict[str, object]) -> None:
    insert(seeded_db, table, {**ROWS[table], **override})


def test_every_table_is_strict(db: sqlite3.Connection) -> None:
    strict = {row["name"]: row["strict"] for row in db.execute("PRAGMA table_list") if row["schema"] == "main" and row["type"] == "table"}
    assert {name: flag for name, flag in strict.items() if not name.startswith("sqlite_")} == dict.fromkeys(ROWS, 1)


@pytest.mark.parametrize("table", sorted(ROWS))
def test_strict_rejects_wrong_types_in_every_column(seeded_db: sqlite3.Connection, table: str) -> None:
    columns = seeded_db.execute(f"PRAGMA table_info({table})").fetchall()
    pk = [c for c in columns if c["pk"]]
    rowid_alias = pk[0]["name"] if len(pk) == 1 and pk[0]["type"] == "INTEGER" else None
    for column in columns:
        if column["name"] == rowid_alias:
            continue
        with pytest.raises(sqlite3.IntegrityError, match=rf"cannot store \w+ value in {column['type']} column {table}\.{column['name']}$"):
            insert(seeded_db, table, {**ROWS[table], column["name"]: WRONG_TYPE[column["type"]]})
