from collections.abc import Iterable, Mapping

from subnet_tracker.core.settings import SettingError, SettingValue, check_key, parse_setting, setting_state
from subnet_tracker.discovery.www import domain_names
from subnet_tracker.textio.changes import Changes, ImportDiff, ImportMode, SettingChange, Update
from subnet_tracker.textio.model import DomainLine, DuplicateLine, ExportDomain, ExportFile, ExportGroup, ExportRoute, GroupSection, ImportFile, RouteLine, SettingLine, network_order
from subnet_tracker.types import DomainName, ParseError, ParseResult

type _RouteKey = tuple[int, int, int]


def _written[T](value: T | None, kept: T) -> T:
    return kept if value is None else value


# the base is the stored entity in merge and the schema defaults in replace, so the same rule gives both modes
def _domain(base: ExportDomain, line: DomainLine) -> ExportDomain:
    prefixes = _written(line.prefix_v4, base.prefix_v4), _written(line.prefix_v6, base.prefix_v6)
    return ExportDomain(base.name, base.groups.union(line.groups), base.tags.union(line.tags), *prefixes, base.enabled and line.enabled, line.comment or base.comment)


def _route(base: ExportRoute, line: RouteLine) -> ExportRoute:
    return ExportRoute(base.network, base.groups.union(line.groups), base.enabled and line.enabled, line.comment or base.comment)


def _group(base: ExportGroup, section: GroupSection | None) -> ExportGroup:
    if section is None:
        return base
    prefixes = _written(section.prefix_v4, base.prefix_v4), _written(section.prefix_v6, base.prefix_v6)
    return ExportGroup(base.name, _written(section.description, base.description), *prefixes, _written(section.enabled, base.enabled))


def _changes[K: str | _RouteKey, T](current: Mapping[K, T], target: Mapping[K, T], mode: ImportMode) -> Changes[T]:
    added = tuple(target[k] for k in sorted(target.keys() - current.keys()))
    updated = tuple(Update(current[k], target[k]) for k in sorted(target.keys() & current.keys()) if current[k] != target[k])
    removed = tuple(current[k] for k in sorted(current.keys() - target.keys())) if mode is ImportMode.REPLACE else ()
    return Changes(added, updated, removed)


def _domains(current: Iterable[ExportDomain], lines: Iterable[DomainLine], mode: ImportMode) -> tuple[dict[DomainName, ExportDomain], list[DuplicateLine]]:
    # merge finds the stored domain by either name (9.4) and keeps its canonical name; replace needs both names and takes the canonical one from the file
    owners = {n: d for d in current for n in domain_names(d.name).all}
    targets: dict[DomainName, ExportDomain] = {}
    first_lines: dict[DomainName, int] = {}
    duplicates = []
    for line in lines:
        names = domain_names(line.name).all
        stored = next((owners[n] for n in names if n in owners), None)
        if stored is not None and mode is ImportMode.REPLACE and set(domain_names(stored.name).all) != set(names):
            stored = None
        key = line.name if stored is None else stored.name
        if first := first_lines.get(key):
            duplicates.append(DuplicateLine(line.line, line.name, first))
        else:
            first_lines[key] = line.line
            targets[key] = _domain(stored if stored is not None and mode is ImportMode.MERGE else ExportDomain(line.name), line)
    return targets, duplicates


def _default(key: str) -> SettingValue | SettingError:
    state = setting_state(key, None)
    return state if isinstance(state, SettingError) else parse_setting(key, state.default)


def _settings(current: Mapping[str, str], lines: Iterable[SettingLine], mode: ImportMode) -> tuple[tuple[SettingChange, ...], list[ParseError]]:
    wanted: dict[str, SettingValue] = {}
    errors = []
    for line in lines:
        match parse_setting(line.key, line.value):
            case SettingError(key, message):
                errors.append(ParseError(line.line, f"{key}: {message}" if check_key(key) is None else message))
            case value:
                wanted[value.key] = value
    if mode is ImportMode.REPLACE:
        wanted |= {key: value for key in current.keys() - wanted.keys() if isinstance(value := _default(key), SettingValue)}
    changes = tuple(SettingChange(key, current.get(key), value) for key, value in sorted(wanted.items()) if current.get(key) != (None if value.is_default else value.text))
    return changes, errors


def _routes(current: Mapping[_RouteKey, ExportRoute], lines: Iterable[RouteLine], mode: ImportMode) -> dict[_RouteKey, ExportRoute]:
    bases = current if mode is ImportMode.MERGE else {}
    keyed = {network_order(line.network): line for line in lines}
    return {key: _route(bases.get(key, ExportRoute(line.network)), line) for key, line in keyed.items()}


def _groups(current: Mapping[str, ExportGroup], sections: Iterable[GroupSection], referenced: set[str], mode: ImportMode) -> dict[str, ExportGroup]:
    # a group referenced by @X or a domain is in the file even without its [group "X"] section
    bases = current if mode is ImportMode.MERGE else {}
    written = {s.name: s for s in sections}
    return {name: _group(bases.get(name, ExportGroup(name)), written.get(name)) for name in written.keys() | referenced}


def import_diff(current: ExportFile, file: ImportFile, mode: ImportMode) -> ParseResult[ImportDiff]:
    settings, errors = _settings({s.key: s.value for s in current.settings}, file.settings, mode)
    domains, duplicates = _domains(current.domains, file.domains, mode)
    stored_routes = {network_order(r.network): r for r in current.routes}
    routes = _routes(stored_routes, file.routes, mode)
    stored_groups = {g.name: g for g in current.groups}
    referenced = {name for d in domains.values() for name in d.groups} | {name for r in routes.values() for name in r.groups}
    diff = ImportDiff(
        settings,
        _changes(stored_groups, _groups(stored_groups, file.groups, referenced, mode), mode),
        _changes({d.name: d for d in current.domains}, domains, mode),
        _changes(stored_routes, routes, mode),
        tuple(sorted([*file.duplicates, *duplicates], key=lambda d: d.line)),
    )
    return ParseResult(diff, tuple(errors))
