import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from subnet_tracker.core.batches import AddResult, add_names, add_text
from subnet_tracker.core.db import transaction
from subnet_tracker.core.domain_view import DomainView, domain_views
from subnet_tracker.core.domains import Domain, DomainEdit, checked_edit, delete_domains, edit_domains, flip_canonical, get_domains, list_domains
from subnet_tracker.core.edits import edit_columns
from subnet_tracker.core.events import EventKind, NewEvent, record_events
from subnet_tracker.core.groups import edit_domain_groups, find_group
from subnet_tracker.core.memberships import SetChange
from subnet_tracker.core.requests import Apply, Request, ResolveDomains, enqueue
from subnet_tracker.core.tags import checked_tag_names, edit_domain_tags, find_tags, insert_tags
from subnet_tracker.types import BatchId, BatchKind, DomainId, DomainName, FieldError, GroupId, JsonValue, ReconnectMode, ResolveTrigger, Timestamp


@dataclass(frozen=True, slots=True)
class DomainChange:
    edit: DomainEdit = field(default_factory=DomainEdit)
    # groups are given by name and must exist; tags are created on first use, a missing tag to remove is skipped
    add_groups: tuple[str, ...] = ()
    remove_groups: tuple[str, ...] = ()
    add_tags: tuple[str, ...] = ()
    remove_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AddOptions:
    # apply.after_add from the web UI, auto from the CLI (8.5)
    reconnect: ReconnectMode
    resolve: bool = True
    # settings of the new domains; duplicates keep theirs
    change: DomainChange = field(default_factory=DomainChange)


type Changes = Mapping[str, tuple[JsonValue, JsonValue]]


@dataclass(frozen=True, slots=True)
class DomainChanged:
    domain: Domain
    # field: (old, new)
    changes: Changes

    @property
    def details(self) -> dict[str, JsonValue]:
        return {name: {"old": old, "new": new} for name, (old, new) in self.changes.items()}


@dataclass(frozen=True, slots=True)
class _Checked:
    edit: DomainEdit
    groups: SetChange[GroupId]
    add_tags: tuple[str, ...]
    remove_tags: tuple[str, ...]


def added_requests(ids: Collection[DomainId], options: AddOptions) -> list[Request]:
    if not ids:
        return []
    resolve: list[Request] = [ResolveDomains(frozenset(ids), ResolveTrigger.NEW_DOMAINS)] if options.resolve else []
    return [*resolve, Apply(options.reconnect)]


def edits(domain: Domain, edit: DomainEdit) -> bool:
    current = {"prefix_v4": domain.prefix_v4, "prefix_v6": domain.prefix_v6, "enabled": domain.enabled, "comment": domain.comment}
    return any(current[column] != value for column, value in edit_columns(edit).items())


def domain_changes(before: DomainView, after: DomainView) -> Changes:
    b, a = before.domain, after.domain
    values: dict[str, tuple[JsonValue, JsonValue]] = {
        "prefix_v4": (b.prefix_v4, a.prefix_v4),
        "prefix_v6": (b.prefix_v6, a.prefix_v6),
        "enabled": (b.enabled, a.enabled),
        "comment": (b.comment, a.comment),
        "groups": (list(before.groups), list(after.groups)),
        "tags": (list(before.tags), list(after.tags)),
    }
    return {name: pair for name, pair in values.items() if pair[0] != pair[1]}


def _event(kind: EventKind, domain: Domain, actor: str, details: Mapping[str, JsonValue]) -> NewEvent:
    return NewEvent(kind, actor, domain.id, domain.canonical.name, details)


def _tag_names(names: Iterable[str]) -> tuple[str, ...] | FieldError:
    checked = checked_tag_names(names)
    return replace(checked, field="tag") if isinstance(checked, FieldError) else checked


def _group_ids(conn: sqlite3.Connection, names: Iterable[str]) -> frozenset[GroupId] | FieldError:
    ids: set[GroupId] = set()
    for name in names:
        if (group := find_group(conn, name)) is None:
            return FieldError("group", f"{name!r} does not exist")
        ids.add(group.id)
    return frozenset(ids)


def _checked(conn: sqlite3.Connection, change: DomainChange) -> _Checked | FieldError:
    edit = checked_edit(change.edit)
    if isinstance(edit, FieldError):
        return edit
    add_tags = _tag_names(change.add_tags)
    if isinstance(add_tags, FieldError):
        return add_tags
    remove_tags = _tag_names(change.remove_tags)
    if isinstance(remove_tags, FieldError):
        return remove_tags
    add_groups = _group_ids(conn, change.add_groups)
    if isinstance(add_groups, FieldError):
        return add_groups
    remove_groups = _group_ids(conn, change.remove_groups)
    if isinstance(remove_groups, FieldError):
        return remove_groups
    return _Checked(edit, SetChange(remove=remove_groups, add=add_groups), add_tags, remove_tags)


def _edit_memberships(conn: sqlite3.Connection, ids: Collection[DomainId], checked: _Checked, now: Timestamp) -> None:
    edit_domain_groups(conn, ids, checked.groups, now)
    edit_domain_tags(conn, ids, SetChange(remove=find_tags(conn, checked.remove_tags), add=insert_tags(conn, checked.add_tags)), now)


def _add(conn: sqlite3.Connection, actor: str, options: AddOptions, now: Timestamp, write: Callable[[], AddResult]) -> AddResult | FieldError:
    with transaction(conn):
        checked = _checked(conn, options.change)
        if isinstance(checked, FieldError):
            return checked
        result = write()
        domains = tuple(reversed(list_domains(conn, result.batch_id)))
        if domains:
            ids = [d.id for d in domains]
            edit_domains(conn, ids, checked.edit, now)
            _edit_memberships(conn, ids, checked, now)
            record_events(conn, [_event(EventKind.DOMAIN_ADDED, d, actor, {"batch_id": result.batch_id}) for d in domains], now)
            enqueue(conn, added_requests(ids, options), now)
    return result


def add_pasted(conn: sqlite3.Connection, kind: BatchKind, actor: str, text: str, options: AddOptions, now: Timestamp) -> AddResult | FieldError:
    return _add(conn, actor, options, now, lambda: add_text(conn, kind, actor, text, now))


def add_domain_names(conn: sqlite3.Connection, kind: BatchKind, actor: str, text: str, names: Iterable[DomainName], options: AddOptions, now: Timestamp) -> AddResult | FieldError:
    return _add(conn, actor, options, now, lambda: add_names(conn, kind, actor, text, names, now))


def change_domains(conn: sqlite3.Connection, ids: Collection[DomainId], change: DomainChange, actor: str, now: Timestamp) -> tuple[DomainChanged, ...] | FieldError:
    with transaction(conn):
        checked = _checked(conn, change)
        if isinstance(checked, FieldError):
            return checked
        before = domain_views(conn, ids)
        edit_domains(conn, [v.domain.id for v in before if edits(v.domain, checked.edit)], checked.edit, now)
        _edit_memberships(conn, ids, checked, now)
        after = domain_views(conn, ids)
        changed = tuple(DomainChanged(a.domain, changes) for b, a in zip(before, after, strict=True) if (changes := domain_changes(b, a)))
        record_events(conn, [_event(EventKind.DOMAIN_CHANGED, c.domain, actor, c.details) for c in changed], now)
        if changed:
            enqueue(conn, [Apply(ReconnectMode.AUTO)], now)
    return changed


def record_deleted(conn: sqlite3.Connection, domains: Sequence[Domain], actor: str, now: Timestamp, batch_id: BatchId | None = None) -> None:
    details: dict[str, JsonValue] = {} if batch_id is None else {"batch_id": batch_id}
    record_events(conn, [_event(EventKind.DOMAIN_DELETED, d, actor, details) for d in domains], now)
    if domains:
        enqueue(conn, [Apply(ReconnectMode.AUTO)], now)


def remove_domains(conn: sqlite3.Connection, ids: Collection[DomainId], actor: str, now: Timestamp) -> tuple[Domain, ...]:
    with transaction(conn):
        domains = get_domains(conn, ids)
        delete_domains(conn, [d.id for d in domains])
        record_deleted(conn, domains, actor, now)
    return domains


@dataclass(frozen=True, slots=True)
class ImportedDomain:
    name: DomainName
    change: DomainChange


def sync_domain(conn: sqlite3.Connection, domain_id: DomainId, change: DomainChange, canonical: DomainName, actor: str, now: Timestamp) -> DomainChanged | FieldError | None:
    with transaction(conn):
        checked = _checked(conn, change)
        if isinstance(checked, FieldError):
            return checked
        before = domain_views(conn, [domain_id])
        if not before:
            return FieldError("name", "does not exist")
        view = before[0]
        if canonical != view.domain.canonical.name:
            if canonical != view.domain.pair.name:
                return FieldError("name", f"{canonical} is not the pair of {view.domain.canonical.name}")
            flip_canonical(conn, domain_id, now)
        if edits(view.domain, checked.edit):
            edited = edit_domains(conn, [domain_id], checked.edit, now)
            if isinstance(edited, FieldError):
                return edited
        _edit_memberships(conn, [domain_id], checked, now)
        after = domain_views(conn, [domain_id])[0]
        changes = dict(domain_changes(view, after))
        if view.domain.canonical.name != after.domain.canonical.name:
            changes = {"name": (view.domain.canonical.name, after.domain.canonical.name), **changes}
        if not changes:
            return None
        changed = DomainChanged(after.domain, changes)
        record_events(conn, [_event(EventKind.DOMAIN_CHANGED, changed.domain, actor, changed.details)], now)
        enqueue(conn, [Apply(ReconnectMode.AUTO)], now)
    return changed


def add_imported_domains(conn: sqlite3.Connection, actor: str, text: str, specs: Sequence[ImportedDomain], now: Timestamp) -> tuple[Domain, ...] | FieldError:
    if not specs:
        return ()
    with transaction(conn):
        checked: list[_Checked] = []
        for spec in specs:
            item = _checked(conn, spec.change)
            if isinstance(item, FieldError):
                return item
            checked.append(item)
        result = add_names(conn, BatchKind.IMPORT, actor, text, [spec.name for spec in specs], now)
        stored = {domain.canonical.name: domain for domain in list_domains(conn, result.batch_id)}
        for spec, item in zip(specs, checked, strict=True):
            domain = stored[spec.name]
            if edits(domain, item.edit):
                edited = edit_domains(conn, [domain.id], item.edit, now)
                if isinstance(edited, FieldError):
                    return edited
            _edit_memberships(conn, [domain.id], item, now)
        found = {domain.canonical.name: domain for domain in list_domains(conn, result.batch_id)}
        domains = tuple(found[spec.name] for spec in specs)
        record_events(conn, [_event(EventKind.DOMAIN_ADDED, domain, actor, {"batch_id": result.batch_id}) for domain in domains], now)
        enqueue(conn, added_requests([domain.id for domain in domains], AddOptions(ReconnectMode.AUTO)), now)
    return domains
