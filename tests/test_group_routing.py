import sqlite3

from db_rows import write_locked
from domains_world import NOW
from groups_world import domain_ids, group_ids, join, make_group
from subnet_tracker.core.domains import DomainEdit, edit_domains
from subnet_tracker.core.groups import GroupEdit, delete_group, edit_domain_groups, edit_group
from subnet_tracker.core.memberships import SetChange
from subnet_tracker.core.routing import routable_domains, routing_domains, routing_groups
from subnet_tracker.routes.model import Domain, Group, PlanSettings, Prefix, PrefixSource
from subnet_tracker.routes.plan import effective_prefix


def test_routing_snapshot_of_domains_and_groups(db: sqlite3.Connection) -> None:
    video, off = make_group(db, "Video", prefix_v4=24), make_group(db, "Off", enabled=False, prefix_v6=40)
    a, b = domain_ids(db, "a.com b.com")
    join(db, a, video, off)
    edit_domains(db, [b], DomainEdit(prefix_v4=20, prefix_v6=36, enabled=False), NOW)
    assert routing_groups(db) == (Group(video.id, True, 24, None), Group(off.id, False, None, 40))
    assert routing_domains(db) == (Domain(a, True, group_ids(video, off)), Domain(b, False, frozenset(), 20, 36))


def test_routable_domains_read_while_another_connection_writes(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    (a,) = domain_ids(db, "a.com")
    with write_locked(db, other):
        assert routable_domains(db) == {a}


def test_domain_without_groups_is_routed_unless_disabled(db: sqlite3.Connection) -> None:
    a, b = domain_ids(db, "a.com b.com")
    edit_domains(db, [b], DomainEdit(enabled=False), NOW)
    assert routable_domains(db) == {a}


def test_domain_is_routed_if_at_least_one_of_its_groups_is_enabled(db: sqlite3.Connection) -> None:
    on, off = make_group(db, "On"), make_group(db, "Off", enabled=False)
    only_off, both, only_on, disabled = domain_ids(db, "off.com both.com on.com disabled.com")
    join(db, only_off, off)
    join(db, both, on, off)
    join(db, only_on, on)
    join(db, disabled, on)
    edit_domains(db, [disabled], DomainEdit(enabled=False), NOW)
    assert routable_domains(db) == {both, only_on}


def test_disabling_and_enabling_a_group_switches_its_domains(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video")
    a, b = domain_ids(db, "a.com b.com")
    join(db, a, video)
    edit_group(db, video.id, GroupEdit(enabled=False), NOW)
    assert routable_domains(db) == {b}
    edit_group(db, video.id, GroupEdit(enabled=True), NOW)
    assert routable_domains(db) == {a, b}


def test_leaving_the_only_disabled_group_makes_a_domain_routed_as_no_group(db: sqlite3.Connection) -> None:
    off, other = make_group(db, "Off", enabled=False), make_group(db, "Other", enabled=False)
    a, b = domain_ids(db, "a.com b.com")
    join(db, a, off)
    join(db, b, other)
    edit_domain_groups(db, [a], SetChange(remove=group_ids(off)), NOW)
    assert routable_domains(db) == {a}
    delete_group(db, other.id)
    assert routable_domains(db) == {a, b}


def test_snapshot_prefix_inheritance_widest_enabled_group_lowest_id_on_tie(db: sqlite3.Connection) -> None:
    first, second, narrow = make_group(db, "First", prefix_v4=20), make_group(db, "Second", prefix_v4=20), make_group(db, "Narrow", prefix_v4=24)
    off = make_group(db, "Off", enabled=False, prefix_v4=16)
    a, b = domain_ids(db, "a.com b.com")
    join(db, a, second, narrow, off, first)
    join(db, b, off)
    groups = {g.id: g for g in routing_groups(db)}
    domain_a, domain_b = routing_domains(db)
    assert effective_prefix(domain_a, groups, PlanSettings(), 4) == Prefix(20, PrefixSource.GROUP, first.id)
    assert effective_prefix(domain_a, groups, PlanSettings(), 6) == Prefix(48, PrefixSource.DEFAULT)
    assert effective_prefix(domain_b, groups, PlanSettings(), 4) == Prefix(23, PrefixSource.DEFAULT)
