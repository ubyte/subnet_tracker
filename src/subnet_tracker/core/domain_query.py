import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum, auto

from subnet_tracker.core.batches import batch_exists
from subnet_tracker.core.db import snapshot
from subnet_tracker.core.domain_view import DomainView, domain_views
from subnet_tracker.core.groups import find_group
from subnet_tracker.core.name_status import NameStatus
from subnet_tracker.core.settings_store import load_settings
from subnet_tracker.core.tags import find_tag
from subnet_tracker.types import BatchId, FieldError, Seconds, Timestamp


class DomainState(StrEnum):
    OK = auto()
    ERROR = auto()
    STALE = auto()
    DISABLED = auto()


class DomainSort(StrEnum):
    LAST_ATTEMPT = "last-attempt"
    LAST_SUCCESS = "last-success"
    NAME = auto()


@dataclass(frozen=True, slots=True)
class DomainQuery:
    group: str | None = None
    no_group: bool = False
    tag: str | None = None
    batch_id: BatchId | None = None
    search: str | None = None
    state: DomainState | None = None
    # None keeps the newest first
    sort: DomainSort | None = None


def in_state(view: DomainView, state: DomainState, retention: Seconds) -> bool:
    match state:
        case DomainState.OK:
            return view.status is NameStatus.OK
        case DomainState.ERROR:
            return view.status in (NameStatus.NOTFOUND, NameStatus.ERROR)
        case DomainState.STALE:
            return view.stale(retention)
        case DomainState.DISABLED:
            return not view.routable


def _found(view: DomainView, text: str) -> bool:
    needle = text.casefold()
    return any(needle in s.casefold() for s in (*(n.name.name for n in view.names), *view.groups, *view.tags))


def matches(view: DomainView, q: DomainQuery, retention: Seconds) -> bool:
    return (
        (q.group is None or q.group in view.groups)
        and not (q.no_group and view.groups)
        and (q.tag is None or q.tag in view.tags)
        and (q.batch_id is None or view.domain.batch_id == q.batch_id)
        and (q.search is None or _found(view, q.search))
        and (q.state is None or in_state(view, q.state, retention))
    )


# never first, then the oldest
def _latest(times: Iterable[Timestamp | None]) -> tuple[bool, int]:
    present = [t for t in times if t is not None]
    return bool(present), max(present, default=0)


def select_domains(views: Sequence[DomainView], query: DomainQuery, retention: Seconds) -> tuple[DomainView, ...]:
    found = [v for v in views if matches(v, query, retention)]
    match query.sort:
        case DomainSort.NAME:
            found.sort(key=lambda v: v.domain.canonical.name)
        case DomainSort.LAST_ATTEMPT:
            found.sort(key=lambda v: _latest(n.name.last_attempt_at for n in v.names))
        case DomainSort.LAST_SUCCESS:
            found.sort(key=lambda v: _latest(n.name.last_success_at for n in v.names))
    return tuple(found)


def query_error(conn: sqlite3.Connection, query: DomainQuery) -> FieldError | None:
    if query.group is not None and find_group(conn, query.group) is None:
        return FieldError("group", f"group {query.group!r} does not exist")
    if query.tag is not None and find_tag(conn, query.tag) is None:
        return FieldError("tag", f"tag {query.tag!r} does not exist")
    if query.batch_id is not None and not batch_exists(conn, query.batch_id):
        return FieldError("batch", f"batch {query.batch_id} does not exist")
    return None


def query_domains(conn: sqlite3.Connection, query: DomainQuery) -> tuple[DomainView, ...] | FieldError:
    with snapshot(conn):
        if error := query_error(conn, query):
            return error
        return select_domains(domain_views(conn), query, load_settings(conn).routes.retention)
