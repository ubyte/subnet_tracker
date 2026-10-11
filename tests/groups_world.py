import sqlite3

from domains_world import NOW, paste
from subnet_tracker.core.domains import Domain, find_domain
from subnet_tracker.core.groups import Group, NewGroup, create_group
from subnet_tracker.types import DomainId, DomainName, GlobalRouteId, GroupId, Timestamp


def make_group(db: sqlite3.Connection, name: str, enabled: bool = True, prefix_v4: int | None = None, prefix_v6: int | None = None, now: Timestamp = NOW) -> Group:
    group = create_group(db, NewGroup(name, prefix_v4=prefix_v4, prefix_v6=prefix_v6, enabled=enabled), now)
    assert isinstance(group, Group)
    return group


def domain(db: sqlite3.Connection, name: str) -> Domain:
    found = find_domain(db, DomainName(name))
    assert found is not None
    return found


def domain_ids(db: sqlite3.Connection, text: str) -> list[DomainId]:
    paste(db, text)
    return [domain(db, name).id for name in text.split()]


def join(db: sqlite3.Connection, domain_id: DomainId, *groups: Group) -> None:
    db.executemany("INSERT INTO domain_groups (domain_id, group_id) VALUES (?, ?)", [(domain_id, g.id) for g in groups])


def route(db: sqlite3.Connection, cidr: str, *groups: Group) -> GlobalRouteId:
    route_id = GlobalRouteId(db.execute("INSERT INTO global_routes (cidr, family, created_at, updated_at) VALUES (?, 4, 0, 0) RETURNING id", (cidr,)).fetchone()["id"])
    db.executemany("INSERT INTO route_groups (route_id, group_id) VALUES (?, ?)", [(route_id, g.id) for g in groups])
    return route_id


def group_names(db: sqlite3.Connection, domain_id: DomainId) -> set[str]:
    return {name for (name,) in db.execute("SELECT g.name FROM domain_groups dg JOIN groups g ON g.id = dg.group_id WHERE dg.domain_id = ?", (domain_id,))}


def group_ids(*groups: Group) -> frozenset[GroupId]:
    return frozenset(g.id for g in groups)
