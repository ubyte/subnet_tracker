from collections.abc import Callable, Hashable, Mapping

from subnet_tracker.discovery.www import domain_names
from subnet_tracker.textio.changes import Changes, ImportDiff
from subnet_tracker.textio.model import ExportDomain, ExportFile, ExportSetting


def _applied[K: Hashable, T](current: tuple[T, ...], changes: Changes[T], key: Callable[[T], K]) -> tuple[T, ...]:
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


def apply_domains(domains: Mapping[int, ExportDomain], changes: Changes[ExportDomain]) -> dict[int, ExportDomain]:
    # removals go first: in replace a new domain may take a name of a removed one
    ids = {d.name: i for i, d in domains.items()}
    result = dict(domains)
    for d in changes.removed:
        assert result.pop(ids[d.name]) == d
    for update in changes.updated:
        assert result[ids[update.old.name]] == update.old
        assert set(domain_names(update.old.name).all) == set(domain_names(update.new.name).all), "an update keeps both names and may only flip names.canonical"
        result[ids[update.old.name]] = update.new
    next_id = max(domains, default=0) + 1
    for offset, d in enumerate(changes.added):
        result[next_id + offset] = d
    return result


def apply_diff(current: ExportFile, diff: ImportDiff) -> ExportFile:
    """What the repositories do with a diff, checking the schema constraints the DB would enforce; addresses live as long as the id of their domain."""
    settings = {s.key: s.value for s in current.settings}
    for change in diff.settings:
        assert settings.pop(change.key, None) == change.old
        if not change.new.is_default:
            settings[change.key] = change.new.text
    groups = _applied(current.groups, diff.groups, lambda g: g.name)
    before = dict(enumerate(current.domains, 1))
    after = apply_domains(before, diff.domains)
    domains = tuple(after.values())
    routes = _applied(current.routes, diff.routes, lambda r: r.network)
    names = [n for d in domains for n in domain_names(d.name).all]
    assert len(names) == len(set(names)), "names.name is UNIQUE"
    existing = {g.name for g in groups}
    assert all(d.groups <= existing for d in domains), "domain_groups reference groups"
    assert all(r.groups <= existing for r in routes), "route_groups reference groups"
    kept = {n for i, d in before.items() if i in after for n in domain_names(d.name).all}
    addresses = None if current.addresses is None else tuple(a for a in current.addresses if a.name in kept)
    return ExportFile(tuple(ExportSetting(k, v) for k, v in settings.items()), groups, domains, routes, addresses)
