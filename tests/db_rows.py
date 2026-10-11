import sqlite3
from collections.abc import Mapping

type Row = Mapping[str, object]

SHA256 = "0" * 64
NET_FIRST, NET_LAST = bytes([198, 51, 100, 0]), bytes([198, 51, 100, 255])

# Parent rows with id 1 that every row in ROWS may reference; domain 2 has no names yet.
PARENTS: tuple[tuple[str, Row], ...] = (
    ("groups", {"id": 1, "name": "parent", "created_at": 0, "updated_at": 0}),
    ("add_batches", {"id": 1, "created_at": 0, "kind": "cli", "actor": "cli:root", "input": "example.com", "added": 1, "duplicates": 0, "invalid": "[]"}),
    ("domains", {"id": 1, "source": "manual", "batch_id": 1, "created_at": 0, "updated_at": 0}),
    ("domains", {"id": 2, "source": "manual", "created_at": 0, "updated_at": 0}),
    ("names", {"id": 1, "domain_id": 1, "name": "example.com", "canonical": 1}),
    ("tags", {"id": 1, "name": "parent"}),
    ("resolvers", {"id": 1, "name": "parent", "kind": "dns", "endpoint": "udp://1.1.1.1", "position": 0, "created_at": 0, "updated_at": 0}),
    ("addresses", {"id": 1, "name_id": 1, "ip": "192.0.2.1", "family": 4, "ip_bin": bytes([192, 0, 2, 1])}),
    ("global_routes", {"id": 1, "cidr": "198.51.100.0/24", "family": 4, "created_at": 0, "updated_at": 0}),
    ("plan_routes", {"id": 1, "cidr": "198.51.100.0/24", "family": 4, "first_bin": NET_FIRST, "last_bin": NET_LAST, "kind": "global", "global_route_id": 1, "added_at": 0, "reconnected": 1}),
    ("resolve_runs", {"id": 1, "trigger": "timer", "started_at": 0}),
)

# A valid row for every table that does not collide with PARENTS.
ROWS: Mapping[str, Row] = {
    "groups": {"name": "g", "created_at": 0, "updated_at": 0},
    "add_batches": {"created_at": 0, "kind": "paste", "actor": "web:10.8.0.2", "input": "", "added": 0, "duplicates": 0, "invalid": "[]"},
    "domains": {"source": "discovered", "source_url": "https://example.com/", "created_at": 0, "updated_at": 0},
    "names": {"domain_id": 2, "name": "example.org", "canonical": 1},
    "domain_groups": {"domain_id": 1, "group_id": 1},
    "tags": {"name": "t"},
    "domain_tags": {"domain_id": 1, "tag_id": 1},
    "resolvers": {"name": "r", "kind": "system", "position": 1, "created_at": 0, "updated_at": 0},
    "agents": {"resolver_id": 1, "cert_sha256": SHA256, "cert_not_after": 0, "created_at": 0},
    "name_resolves": {"name_id": 1, "resolver_id": 1, "attempted_at": 0, "status": "ok", "cnames": "[]"},
    "resolve_leases": {"name_id": 1, "resolver_id": 1, "leased_until": 0},
    "addresses": {"name_id": 1, "ip": "2001:db8::1", "family": 6, "ip_bin": bytes(16)},
    "sightings": {"address_id": 1, "resolver_id": 1, "first_seen_at": 0, "last_seen_at": 0},
    "global_routes": {"cidr": "203.0.113.0/24", "family": 4, "created_at": 0, "updated_at": 0},
    "route_groups": {"route_id": 1, "group_id": 1},
    "plan_state": {"id": 1, "computed_at": 0, "plan_sha256": SHA256, "routes_v4": 0, "routes_v6": 0, "raw_v4": 0, "raw_v6": 0, "extra_ratio_v4": 0.0, "extra_ratio_v6": 0.0, "warnings": "[]"},
    "plan_routes": {"cidr": "192.0.2.0/24", "family": 4, "first_bin": bytes([192, 0, 2, 0]), "last_bin": bytes([192, 0, 2, 255]), "kind": "dns", "added_at": 0, "reconnected": 0},
    "plan_route_parts": {"route_id": 1, "cidr": "198.51.100.0/24", "first_bin": NET_FIRST, "last_bin": NET_LAST},
    "settings": {"key": "routes.retention", "value": '"14d"', "updated_at": 0},
    "scheduler_state": {"id": 1},
    "reconnect_state": {"id": 1},
    "ovpn_clients": {"instance": "server", "cid": 1, "common_name": "laptop", "real_address": "203.0.113.5:1194", "connected_at": 0, "snapshot_at": 0},
    "web_devices": {"token_sha256": SHA256, "user_agent": "", "created_ip": "10.8.0.2", "created_at": 0, "last_seen_at": 0, "last_seen_ip": "10.8.0.2"},
    "requests": {"kind": "apply", "created_at": 0},
    "resolve_runs": {"trigger": "manual", "started_at": 0},
    "resolve_run_resolvers": {"run_id": 1, "resolver_id": 1, "queries": 0, "ok": 0, "notfound": 0, "errors": 0, "unreachable": 0, "healthy": 1},
    "apply_runs": {
        "trigger": "tick",
        "started_at": 0,
        "routes_v4": 0,
        "routes_v6": 0,
        "routes_added": 0,
        "routes_removed": 0,
        "diff": '{"added": [], "removed": []}',
        "file_sha256": SHA256,
        "file_changed": 0,
        "reconnect": "none",
        "status": "unchanged",
    },
    "events": {"ts": 0, "kind": "domain.added", "actor": "tick"},
}


def insert(conn: sqlite3.Connection, table: str, row: Row) -> int:
    cursor = conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def seed(conn: sqlite3.Connection) -> None:
    for table, row in PARENTS:
        insert(conn, table, row)


def tables(conn: sqlite3.Connection) -> set[str]:
    return {name for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
