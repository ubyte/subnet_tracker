import json
import sqlite3

from subnet_tracker.core.db import transaction
from subnet_tracker.routes.model import Domain, Group
from subnet_tracker.routes.plan import routable
from subnet_tracker.types import DomainId, GroupId

_DOMAINS = "SELECT id, enabled, prefix_v4, prefix_v6, (SELECT json_group_array(group_id) FROM domain_groups WHERE domain_id = d.id) AS groups FROM domains d ORDER BY id"


def routing_groups(conn: sqlite3.Connection) -> tuple[Group, ...]:
    return tuple(Group(GroupId(row["id"]), bool(row["enabled"]), row["prefix_v4"], row["prefix_v6"]) for row in conn.execute("SELECT id, enabled, prefix_v4, prefix_v6 FROM groups ORDER BY id"))


def routing_domains(conn: sqlite3.Connection) -> tuple[Domain, ...]:
    return tuple(Domain(DomainId(row["id"]), bool(row["enabled"]), frozenset(GroupId(g) for g in json.loads(row["groups"])), row["prefix_v4"], row["prefix_v6"]) for row in conn.execute(_DOMAINS))


def routable_domains(conn: sqlite3.Connection) -> frozenset[DomainId]:
    with transaction(conn):
        groups = {g.id: g for g in routing_groups(conn)}
        domains = routing_domains(conn)
    return frozenset(d.id for d in domains if routable(d.enabled, d.groups, groups))
