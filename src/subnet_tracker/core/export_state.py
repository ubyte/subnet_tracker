import sqlite3
from collections.abc import Sequence
from ipaddress import ip_address

from subnet_tracker.core.db import snapshot
from subnet_tracker.core.domain_view import DomainView, domain_views
from subnet_tracker.core.global_routes import list_global_routes
from subnet_tracker.core.groups import Group, list_groups
from subnet_tracker.core.settings import SettingError, StoredSetting, check_key, setting_state
from subnet_tracker.textio.model import ExportAddress, ExportDomain, ExportFile, ExportGroup, ExportRoute, ExportSetting
from subnet_tracker.types import Timestamp


def _settings(conn: sqlite3.Connection) -> tuple[ExportSetting, ...]:
    found = []
    for row in conn.execute("SELECT key, value FROM settings ORDER BY key"):
        if check_key(row["key"]) is not None:
            continue
        state = setting_state(row["key"], StoredSetting(row["value"], Timestamp(0)))
        if isinstance(state, SettingError):
            continue
        found.append(ExportSetting(state.key, state.value))
    return tuple(found)


def _addresses(views: Sequence[DomainView]) -> tuple[ExportAddress, ...]:
    rows: list[ExportAddress] = []
    for view in views:
        for name in view.names:
            for address in name.addresses:
                ip = ip_address(address.ip)
                rows.extend(ExportAddress(name.name.name, ip, sighting.resolver, sighting.first_seen_at, sighting.last_seen_at) for sighting in address.sightings)
    return tuple(rows)


def _domain(view: DomainView) -> ExportDomain:
    domain = view.domain
    return ExportDomain(domain.canonical.name, frozenset(view.groups), frozenset(view.tags), domain.prefix_v4, domain.prefix_v6, domain.enabled, domain.comment)


def _groups(groups: Sequence[Group]) -> tuple[ExportGroup, ...]:
    return tuple(ExportGroup(group.name, group.description, group.prefix_v4, group.prefix_v6, group.enabled) for group in groups)


def read_export(conn: sqlite3.Connection, *, addresses: bool = False) -> ExportFile:
    with snapshot(conn):
        settings = _settings(conn)
        groups = [row.group for row in list_groups(conn) if row.group is not None]
        names = {group.id: group.name for group in groups}
        views = domain_views(conn)
        domains = tuple(_domain(view) for view in views)
        routes = tuple(ExportRoute(route.network, frozenset(names[group_id] for group_id in route.groups if group_id in names), route.enabled, route.comment) for route in list_global_routes(conn))
        exported = _addresses(views) if addresses else None
    return ExportFile(settings, _groups(groups), domains, routes, exported)
