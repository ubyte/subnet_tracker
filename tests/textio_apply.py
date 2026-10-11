from collections.abc import Callable, Hashable

from subnet_tracker.discovery.www import domain_names
from subnet_tracker.textio.changes import Changes, ImportDiff
from subnet_tracker.textio.model import ExportFile, ExportSetting


def _applied[K: Hashable, T](current: tuple[T, ...], changes: Changes[T], key: Callable[[T], K]) -> tuple[T, ...]:
    # removals go first: in replace a new domain may take a name of a removed one
    items = {key(item): item for item in current}
    for item in changes.removed:
        assert items.pop(key(item)) == item
    for update in changes.updated:
        assert key(update.old) == key(update.new)
        assert items[key(update.old)] == update.old
        items[key(update.new)] = update.new
    for item in changes.added:
        assert key(item) not in items
        items[key(item)] = item
    return tuple(items.values())


def apply_diff(current: ExportFile, diff: ImportDiff) -> ExportFile:
    """What the repositories do with a diff, checking the schema constraints the DB would enforce."""
    settings = {s.key: s.value for s in current.settings}
    for change in diff.settings:
        assert settings.pop(change.key, None) == change.old
        if not change.new.is_default:
            settings[change.key] = change.new.text
    groups = _applied(current.groups, diff.groups, lambda g: g.name)
    domains = _applied(current.domains, diff.domains, lambda d: d.name)
    routes = _applied(current.routes, diff.routes, lambda r: r.network)
    names = [n for d in domains for n in domain_names(d.name).all]
    assert len(names) == len(set(names)), "names.name is UNIQUE"
    existing = {g.name for g in groups}
    assert all(d.groups <= existing for d in domains), "domain_groups reference groups"
    assert all(r.groups <= existing for r in routes), "route_groups reference groups"
    return ExportFile(tuple(ExportSetting(k, v) for k, v in settings.items()), groups, domains, routes)
