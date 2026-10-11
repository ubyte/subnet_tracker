import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace

from subnet_tracker.core.db import transaction
from subnet_tracker.core.domains import DomainEdit, find_domain
from subnet_tracker.core.edits import KEEP
from subnet_tracker.core.export_state import read_export
from subnet_tracker.core.global_routes import GlobalRouteEdit, find_global_route
from subnet_tracker.core.groups import NO_GROUP, GroupEdit, NewGroup, find_group
from subnet_tracker.core.services.domains import DomainChange, ImportedDomain, add_imported_domains, remove_domains, sync_domain
from subnet_tracker.core.services.groups import add_group, change_group, remove_group
from subnet_tracker.core.services.routes import RouteChange, add_route, change_route, remove_route
from subnet_tracker.core.services.settings import change_setting
from subnet_tracker.core.services.tags import remove_tag
from subnet_tracker.core.settings import SettingError
from subnet_tracker.core.tags import find_tag, list_tags
from subnet_tracker.discovery.www import domain_names
from subnet_tracker.routes.exclusions import server_address_error
from subnet_tracker.textio.changes import ImportDiff, ImportMode, import_summary
from subnet_tracker.textio.diff import import_diff
from subnet_tracker.textio.model import ExportDomain, ExportFile, GroupSection, ImportFile, RouteLine, SettingLine
from subnet_tracker.textio.parse import parse_import
from subnet_tracker.types import DomainId, DomainName, FieldError, GroupId, IPAddress, ParseError, Timestamp


class _ApplyError(Exception):
    def __init__(self, error: FieldError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True, slots=True)
class ImportResult:
    diff: ImportDiff
    errors: tuple[ParseError, ...] = ()
    rejected: tuple[FieldError, ...] = ()
    # this call queued Apply(auto); the caller notifies tick only after commit, and only then
    applied: bool = False


def _in_scope(groups: frozenset[str], tags: frozenset[str], wanted_groups: frozenset[str], wanted_tags: frozenset[str]) -> bool:
    if wanted_groups:
        named = wanted_groups - {NO_GROUP}
        if not (bool(named & groups) or (NO_GROUP in wanted_groups and not groups)):
            return False
    return not wanted_tags or bool(tags & wanted_tags)


def _scoped(current: ExportFile, file: ImportFile, groups: frozenset[str], tags: frozenset[str]) -> tuple[ExportFile, ImportFile]:
    if not groups and not tags:
        return current, file
    # a line that names a stored domain outside the filter is not an add: that domain is left untouched
    outside = {name for domain in current.domains if not _in_scope(domain.groups, domain.tags, groups, tags) for name in domain_names(domain.name).all}
    domains = tuple(line for line in file.domains if _in_scope(frozenset(line.groups), frozenset(line.tags), groups, tags) and outside.isdisjoint(domain_names(line.name).all))
    first = {line.line for line in domains}
    duplicates = tuple(dup for dup in file.duplicates if dup.first_line in first)
    kept = tuple(domain for domain in current.domains if _in_scope(domain.groups, domain.tags, groups, tags))
    return replace(current, domains=kept), replace(file, domains=domains, duplicates=duplicates)


def _scope_errors(conn: sqlite3.Connection, groups: Sequence[str], tags: Sequence[str]) -> tuple[FieldError, ...]:
    errors: list[FieldError] = []
    seen: set[str] = set()
    for name in groups:
        if name in seen or name == NO_GROUP:
            seen.add(name)
            continue
        seen.add(name)
        if find_group(conn, name) is None:
            errors.append(FieldError("group", f"{name!r} does not exist"))
    seen.clear()
    for name in tags:
        if name in seen:
            continue
        seen.add(name)
        if find_tag(conn, name) is None:
            errors.append(FieldError("tag", f"{name!r} does not exist"))
    return tuple(errors)


def _server_errors(current: ExportFile, file: ImportFile, servers: Collection[IPAddress]) -> tuple[ParseError, ...]:
    stored = {route.network for route in current.routes}
    errors = []
    for line in file.routes:
        if line.network in stored:
            continue
        if (message := server_address_error(line.network, servers)) is not None:
            errors.append(ParseError(line.line, f"{line.network} {message}"))
    return tuple(errors)


def _keep_omitted(current: ExportFile, file: ImportFile) -> ImportFile:
    # a referenced group is in the file without a section, so replace defaults it; the filter keeps only what the file never mentions
    named = {section.name for section in file.groups} | {name for line in file.domains for name in line.groups} | {name for line in file.routes for name in line.groups}
    groups = tuple(GroupSection(0, group.name, group.description, group.prefix_v4, group.prefix_v6, group.enabled) for group in current.groups if group.name not in named)
    keys = {line.key for line in file.settings}
    settings = tuple(SettingLine(0, setting.key, setting.value) for setting in current.settings if setting.key not in keys)
    networks = {line.network for line in file.routes}
    routes = tuple(RouteLine(0, route.network, tuple(sorted(route.groups)), route.enabled, route.comment) for route in current.routes if route.network not in networks)
    return replace(file, settings=(*settings, *file.settings), groups=(*groups, *file.groups), routes=(*routes, *file.routes))


def _changed(diff: ImportDiff) -> bool:
    summary = import_summary(diff)
    return any(counts.added or counts.updated or counts.removed for counts in (summary.settings, summary.groups, summary.domains, summary.routes))


def _plan(conn: sqlite3.Connection, text: str, mode: ImportMode, groups: Sequence[str], tags: Sequence[str], servers: Collection[IPAddress]) -> ImportResult:
    parsed = parse_import(text)
    current = read_export(conn)
    scoped_current, scoped_file = _scoped(current, parsed.value, frozenset(groups), frozenset(tags))
    if (groups or tags) and mode is ImportMode.REPLACE:
        scoped_file = _keep_omitted(scoped_current, scoped_file)
    diff = import_diff(scoped_current, scoped_file, mode)
    errors = (*parsed.errors, *diff.errors, *_server_errors(current, parsed.value, servers))
    return ImportResult(diff.value, errors, _scope_errors(conn, groups, tags))


def preview_import(conn: sqlite3.Connection, text: str, mode: ImportMode, groups: Sequence[str], tags: Sequence[str], servers: Collection[IPAddress]) -> ImportResult:
    return _plan(conn, text, mode, groups, tags, servers)


def _domain_change(old: ExportDomain, new: ExportDomain) -> DomainChange:
    edit = DomainEdit(new.prefix_v4, new.prefix_v6, new.enabled, new.comment)
    return DomainChange(edit, tuple(sorted(new.groups - old.groups)), tuple(sorted(old.groups - new.groups)), tuple(sorted(new.tags - old.tags)), tuple(sorted(old.tags - new.tags)))


def _ok[T](result: T | FieldError) -> T:
    if isinstance(result, FieldError):
        raise _ApplyError(result)
    return result


def _domain_id(conn: sqlite3.Connection, name: str) -> DomainId:
    found = find_domain(conn, DomainName(name))
    if found is None:
        raise _ApplyError(FieldError("name", f"{name} is not tracked"))
    return found.id


def _group_id(conn: sqlite3.Connection, name: str) -> GroupId:
    found = find_group(conn, name)
    if found is None:
        raise _ApplyError(FieldError("group", f"{name!r} does not exist"))
    return found.id


def _apply(conn: sqlite3.Connection, text: str, mode: ImportMode, diff: ImportDiff, servers: Collection[IPAddress], actor: str, now: Timestamp) -> bool:
    applied = False
    for setting in diff.settings:
        updated = change_setting(conn, setting.key, None if setting.new.is_default else setting.new.text, actor, now)
        if isinstance(updated, SettingError):
            raise _ApplyError(FieldError(updated.key, updated.message))
        applied = applied or updated.changed
    removed = remove_domains(conn, [_domain_id(conn, domain.name) for domain in diff.domains.removed], actor, now)
    applied = applied or bool(removed)
    for group in diff.groups.added:
        created = _ok(add_group(conn, NewGroup(group.name, group.description, group.prefix_v4, group.prefix_v6, group.enabled), actor, now))
        applied = applied or created.applied
    for group_update in diff.groups.updated:
        group_target = group_update.new
        edit = GroupEdit(KEEP, group_target.description, group_target.prefix_v4, group_target.prefix_v6, group_target.enabled)
        edited = _ok(change_group(conn, _group_id(conn, group_update.old.name), edit, actor, now))
        applied = applied or edited.applied
    for domain_update in diff.domains.updated:
        changed = sync_domain(conn, _domain_id(conn, domain_update.old.name), _domain_change(domain_update.old, domain_update.new), domain_update.new.name, actor, now)
        if isinstance(changed, FieldError):
            raise _ApplyError(changed)
        applied = applied or changed is not None
    specs = tuple(ImportedDomain(domain.name, _domain_change(ExportDomain(domain.name), domain)) for domain in diff.domains.added)
    added = _ok(add_imported_domains(conn, actor, text, specs, now))
    applied = applied or bool(added)
    for route in diff.routes.removed:
        stored = find_global_route(conn, route.network)
        if stored is None:
            raise _ApplyError(FieldError("cidr", f"{route.network} does not exist"))
        removed_route = _ok(remove_route(conn, stored.id, actor, now))
        applied = applied or removed_route.applied
    for route_update in diff.routes.updated:
        stored = find_global_route(conn, route_update.old.network)
        if stored is None:
            raise _ApplyError(FieldError("cidr", f"{route_update.old.network} does not exist"))
        route_target = route_update.new
        add_groups = tuple(sorted(route_target.groups - route_update.old.groups))
        remove_groups = tuple(sorted(route_update.old.groups - route_target.groups))
        route_change = RouteChange(GlobalRouteEdit(KEEP, route_target.comment, route_target.enabled), add_groups, remove_groups)
        edited_route = _ok(change_route(conn, stored.id, route_change, servers, actor, now))
        applied = applied or edited_route.applied
    for route in diff.routes.added:
        created_route = _ok(add_route(conn, str(route.network), route.comment, tuple(sorted(route.groups)), servers, actor, now, enabled=route.enabled))
        applied = applied or created_route.applied
    for group in diff.groups.removed:
        removed_group = _ok(remove_group(conn, _group_id(conn, group.name), False, actor, now))
        applied = applied or removed_group.applied
    if mode is ImportMode.REPLACE:
        for row in list_tags(conn):
            if row.domains == 0:
                _ok(remove_tag(conn, row.tag.id, actor, now))
    return applied


def apply_import(conn: sqlite3.Connection, text: str, mode: ImportMode, groups: Sequence[str], tags: Sequence[str], servers: Collection[IPAddress], actor: str, now: Timestamp) -> ImportResult:
    try:
        with transaction(conn):
            plan = _plan(conn, text, mode, groups, tags, servers)
            if plan.errors or plan.rejected or not _changed(plan.diff):
                return plan
            return replace(plan, applied=_apply(conn, text, mode, plan.diff, servers, actor, now))
    except _ApplyError as failed:
        return ImportResult(ImportDiff(), (), (failed.error,))
