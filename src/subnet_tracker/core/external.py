import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address

from subnet_tracker.core.addresses import NameResolution, Resolution
from subnet_tracker.core.db import transaction
from subnet_tracker.core.requests import Apply, enqueue
from subnet_tracker.core.resolvers import get_resolver
from subnet_tracker.core.services.resolves import record_resolutions
from subnet_tracker.core.settings_store import load_settings
from subnet_tracker.resolver.browser_report import BrowserPair, BrowserReportError, ElementRejection, canonical_name, parse_browser_report
from subnet_tracker.resolver.model import NameResult
from subnet_tracker.types import DomainName, IPAddress, NameId, ReconnectMode, ResolverId, ResolverKind, ResolveRunId, ResolveStatus, ResolveTrigger, Seconds, Timestamp

# Issued work stays out of the next selection for five minutes; a session ends after ten minutes without contact (6.5).
LEASE = Seconds(300)
SILENCE = Seconds(600)
MAX_RESULTS = 500
_MAX_FAMILY = 64
_EXTERNAL = frozenset({ResolverKind.BROWSER, ResolverKind.AGENT})
_FAILURE = frozenset({ResolveStatus.ERROR, ResolveStatus.UNREACHABLE})
_CANDIDATES = (
    "SELECT n.id AS name_id, n.name, nr.attempted_at, nr.requested_at, rl.leased_until FROM names n"
    " LEFT JOIN name_resolves nr ON nr.name_id = n.id AND nr.resolver_id = ?"
    " LEFT JOIN resolve_leases rl ON rl.name_id = n.id AND rl.resolver_id = ? ORDER BY n.id"
)
_LEASE = "INSERT INTO resolve_leases (name_id, resolver_id, leased_until) VALUES (?, ?, ?) ON CONFLICT (name_id, resolver_id) DO UPDATE SET leased_until = excluded.leased_until"


@dataclass(frozen=True, slots=True)
class TaskCandidate:
    name_id: NameId
    name: DomainName
    attempted_at: Timestamp | None
    requested_at: Timestamp | None
    leased_until: Timestamp | None


@dataclass(frozen=True, slots=True)
class TaskName:
    name_id: NameId
    name: DomainName


@dataclass(frozen=True, slots=True)
class Assignment:
    names: tuple[TaskName, ...]
    session_closed: bool


@dataclass(frozen=True, slots=True)
class AssignRefusal:
    reason: str


@dataclass(frozen=True, slots=True)
class ReportedResult:
    name: str
    status: str
    ipv4: tuple[str, ...] = ()
    ipv6: tuple[str, ...] = ()
    cnames: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RejectedResult:
    index: int
    reason: str


@dataclass(frozen=True, slots=True)
class Acceptance:
    recorded: int
    rejected: tuple[RejectedResult, ...]


@dataclass(frozen=True, slots=True)
class AcceptRefusal:
    reason: str


@dataclass(frozen=True, slots=True)
class _Ready:
    name_id: NameId
    resolver_id: ResolverId
    resolution: Resolution


def _priority(row: TaskCandidate) -> bool:
    if row.attempted_at is None:
        return True
    return row.requested_at is not None and row.requested_at > row.attempted_at


def _age(row: TaskCandidate) -> tuple[bool, int, int]:
    return (row.attempted_at is not None, 0 if row.attempted_at is None else row.attempted_at, row.name_id)


def select_tasks(candidates: Sequence[TaskCandidate], n: int, max_age: Seconds, now: Timestamp) -> tuple[TaskCandidate, ...]:
    # A lease is active while leased_until is still ahead of now; an attempt is due only once it is older than m.
    available = [row for row in candidates if row.leased_until is None or row.leased_until <= now]
    first = [row for row in available if _priority(row)]
    later = [row for row in available if not _priority(row) and row.attempted_at is not None and now - row.attempted_at > max_age]
    first.sort(key=_age)
    later.sort(key=_age)
    return tuple([*first, *later][:n])


def _stamp(value: int | None) -> Timestamp | None:
    return None if value is None else Timestamp(value)


def _candidates(conn: sqlite3.Connection, resolver_id: ResolverId) -> tuple[TaskCandidate, ...]:
    return tuple(
        TaskCandidate(NameId(row["name_id"]), DomainName(row["name"]), _stamp(row["attempted_at"]), _stamp(row["requested_at"]), _stamp(row["leased_until"]))
        for row in conn.execute(_CANDIDATES, (resolver_id, resolver_id))
    )


def _open_run(conn: sqlite3.Connection, resolver_id: ResolverId) -> tuple[int, int] | None:
    row = conn.execute("SELECT id, started_at FROM resolve_runs WHERE resolver_id = ? AND finished_at IS NULL ORDER BY id DESC LIMIT 1", (resolver_id,)).fetchone()
    return None if row is None else (int(row["id"]), int(row["started_at"]))


def _finish(conn: sqlite3.Connection, run_id: int, finished_at: Timestamp) -> None:
    conn.execute("UPDATE resolve_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))


def _last_activity(conn: sqlite3.Connection, resolver_id: ResolverId, started_at: int) -> int:
    attempted = conn.execute("SELECT max(attempted_at) FROM name_resolves WHERE resolver_id = ? AND attempted_at >= ?", (resolver_id, started_at)).fetchone()[0]
    leased = conn.execute("SELECT max(leased_until) FROM resolve_leases WHERE resolver_id = ?", (resolver_id,)).fetchone()[0]
    # A hand-out is contact: leased_until is the grant time plus the five-minute lease.
    last = started_at
    if isinstance(attempted, int):
        last = max(last, attempted)
    if isinstance(leased, int) and leased - LEASE >= started_at:
        last = max(last, leased - LEASE)
    return last


def _close_if_silent(conn: sqlite3.Connection, resolver_id: ResolverId, now: Timestamp) -> bool:
    row = _open_run(conn, resolver_id)
    if row is None:
        return False
    run_id, started_at = row
    last = _last_activity(conn, resolver_id, started_at)
    if now - last < SILENCE:
        return False
    _finish(conn, run_id, Timestamp(last + SILENCE))
    return True


def _close_if_idle(conn: sqlite3.Connection, resolver_id: ResolverId, now: Timestamp) -> bool:
    row = _open_run(conn, resolver_id)
    if row is None:
        return False
    _finish(conn, row[0], now)
    return True


def _open_session(conn: sqlite3.Connection, resolver_id: ResolverId, now: Timestamp) -> ResolveRunId:
    row = _open_run(conn, resolver_id)
    if row is not None:
        return ResolveRunId(row[0])
    kind = conn.execute("SELECT kind FROM resolvers WHERE id = ?", (resolver_id,)).fetchone()["kind"]
    trigger = ResolveTrigger.BROWSER if kind == ResolverKind.BROWSER else ResolveTrigger.AGENT
    created = conn.execute("INSERT INTO resolve_runs (trigger, resolver_id, started_at) VALUES (?, ?, ?) RETURNING id", (trigger, resolver_id, now)).fetchone()
    return ResolveRunId(created["id"])


def _limits(kind: ResolverKind, conn: sqlite3.Connection) -> tuple[int, Seconds]:
    settings = load_settings(conn)
    if kind is ResolverKind.BROWSER:
        return settings.browser.batch, settings.browser.max_age
    return settings.agent.batch, settings.agent.max_age


def assign_tasks(conn: sqlite3.Connection, resolver_id: ResolverId, now: Timestamp) -> Assignment | AssignRefusal:
    with transaction(conn):
        resolver = get_resolver(conn, resolver_id)
        if resolver is None:
            return AssignRefusal("unknown resolver")
        if resolver.kind not in _EXTERNAL:
            return AssignRefusal("not an external resolver")
        batch, max_age = _limits(resolver.kind, conn)
        closed = _close_if_silent(conn, resolver.id, now)
        chosen = select_tasks(_candidates(conn, resolver.id), batch, max_age, now)
        if not chosen:
            closed = _close_if_idle(conn, resolver.id, now) or closed
        else:
            until = now + LEASE
            conn.executemany(_LEASE, ((item.name_id, resolver.id, until) for item in chosen))
        return Assignment(tuple(TaskName(item.name_id, item.name) for item in chosen), closed)


def _address(text: str, version: int) -> IPAddress | None:
    try:
        parsed = ip_address(text)
    except ValueError:
        return None
    if parsed.version != version or (isinstance(parsed, IPv6Address) and (parsed.scope_id or parsed.ipv4_mapped is not None)):
        return None
    return parsed


def _addresses(texts: Sequence[str], version: int) -> frozenset[IPAddress] | str:
    if len(texts) > _MAX_FAMILY:
        return "more than 64 addresses in one family"
    found: list[IPAddress] = []
    for text in texts:
        parsed = _address(text, version)
        if parsed is None:
            return "invalid address"
        found.append(parsed)
    return frozenset(found)


def _cnames(texts: Sequence[str]) -> tuple[DomainName, ...] | str:
    names: list[DomainName] = []
    for text in texts:
        name = canonical_name(text)
        if name is None:
            return "cname is not in canonical form"
        names.append(name)
    return tuple(names)


def _reported(item: ReportedResult, kind: ResolverKind) -> tuple[DomainName, Resolution] | str:
    name = canonical_name(item.name)
    if name is None:
        return "name is not in canonical form"
    if item.status not in ResolveStatus:
        return "unknown status"
    status = ResolveStatus(item.status)
    if kind is ResolverKind.BROWSER and status is ResolveStatus.UNREACHABLE:
        return "browser results are not unreachable"
    if item.error is not None and status not in _FAILURE:
        return "error text is only for a failure"
    ipv4 = _addresses(item.ipv4, 4)
    if isinstance(ipv4, str):
        return ipv4
    ipv6 = _addresses(item.ipv6, 6)
    if isinstance(ipv6, str):
        return ipv6
    cnames = _cnames(item.cnames)
    if isinstance(cnames, str):
        return cnames
    addresses = ipv4 | ipv6
    if (status is ResolveStatus.OK) != bool(addresses):
        return "status does not match the addresses"
    return name, Resolution(status, addresses, cnames, item.error if status in _FAILURE else None)


def _name_ids(conn: sqlite3.Connection, names: Sequence[str]) -> dict[str, NameId]:
    if not names:
        return {}
    marks = ", ".join("?" * len(names))
    return {row["name"]: NameId(row["id"]) for row in conn.execute(f"SELECT id, name FROM names WHERE name IN ({marks})", tuple(names))}


def _resolver_kinds(conn: sqlite3.Connection, ids: Sequence[int]) -> dict[ResolverId, ResolverKind]:
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    return {ResolverId(row["id"]): ResolverKind(row["kind"]) for row in conn.execute(f"SELECT id, kind FROM resolvers WHERE id IN ({marks})", tuple(ids))}


def _resolution(result: NameResult) -> Resolution:
    # Resolution keeps an error text only on a failure. One good family stays ok; its partner's failure text is not stored.
    status = ResolveStatus.ERROR if result.status is ResolveStatus.UNREACHABLE else result.status
    addresses = frozenset(result.addresses) if status is ResolveStatus.OK else frozenset()
    return Resolution(status, addresses, result.cnames, result.error if status is ResolveStatus.ERROR else None)


def _record(conn: sqlite3.Connection, ready: Sequence[_Ready], reporter: str, now: Timestamp) -> int:
    if not ready:
        return 0
    grouped: dict[ResolverId, list[NameResolution]] = {}
    for item in ready:
        grouped.setdefault(item.resolver_id, []).append(NameResolution(item.name_id, item.resolver_id, item.resolution, reporter))
    recorded = 0
    for resolver_id, items in grouped.items():
        _close_if_silent(conn, resolver_id, now)
        run_id = _open_session(conn, resolver_id, now)
        fresh = record_resolutions(conn, items, reporter, now)
        # External work is per name (6.5), so domains_total counts those name results.
        conn.execute("UPDATE resolve_runs SET domains_total = domains_total + ?, addresses_new = addresses_new + ? WHERE id = ?", (len(items), fresh.addresses_new, run_id))
        conn.executemany("DELETE FROM resolve_leases WHERE name_id = ? AND resolver_id = ?", ((item.name_id, resolver_id) for item in items))
        recorded += len(items)
    # 6.5 asks for an apply request and does not name a reconnect mode; auto is the mode used when plan inputs change.
    # The caller writes the trigger file after this transaction commits.
    enqueue(conn, [Apply(ReconnectMode.AUTO)], now)
    return recorded


def accept_results(conn: sqlite3.Connection, resolver_id: ResolverId, results: Sequence[ReportedResult], reporter: str, now: Timestamp) -> Acceptance | AcceptRefusal:
    if len(results) > MAX_RESULTS:
        return AcceptRefusal("more than 500 results")
    with transaction(conn):
        resolver = get_resolver(conn, resolver_id)
        if resolver is None:
            return AcceptRefusal("unknown resolver")
        if resolver.kind not in _EXTERNAL:
            return AcceptRefusal("not an external resolver")
        rejected: list[RejectedResult] = []
        checked: list[tuple[int, DomainName, Resolution]] = []
        for index, item in enumerate(results):
            parsed = _reported(item, resolver.kind)
            if isinstance(parsed, str):
                rejected.append(RejectedResult(index, parsed))
            else:
                checked.append((index, parsed[0], parsed[1]))
        found = _name_ids(conn, tuple({name for _index, name, _resolution in checked}))
        ready: list[_Ready] = []
        for index, name, resolution in checked:
            name_id = found.get(name)
            if name_id is None:
                rejected.append(RejectedResult(index, "unknown name"))
            else:
                ready.append(_Ready(name_id, resolver.id, resolution))
        rejected.sort(key=lambda item: item.index)
        return Acceptance(_record(conn, ready, reporter, now), tuple(rejected))


def _accept_pairs(conn: sqlite3.Connection, pairs: Sequence[BrowserPair], rejected: Sequence[ElementRejection], reporter: str, now: Timestamp) -> Acceptance:
    found = _name_ids(conn, tuple({pair.name for pair in pairs}))
    kinds = _resolver_kinds(conn, tuple({pair.resolver_id for pair in pairs}))
    extra: list[RejectedResult] = [RejectedResult(item.index, item.reason) for item in rejected]
    ready: list[_Ready] = []
    for pair in pairs:
        kind = kinds.get(pair.resolver_id)
        if kind is None:
            reason = "unknown resolver"
        elif kind is not ResolverKind.BROWSER:
            reason = "resolver is not a browser resolver"
        elif pair.name not in found:
            reason = "unknown name"
        else:
            ready.append(_Ready(found[pair.name], pair.resolver_id, _resolution(pair.result)))
            continue
        extra.extend(RejectedResult(index, reason) for index in pair.indices)
    extra.sort(key=lambda item: item.index)
    return Acceptance(_record(conn, ready, reporter, now), tuple(extra))


def accept_browser_results(conn: sqlite3.Connection, payload: object, reporter: str, now: Timestamp) -> Acceptance | AcceptRefusal:
    parsed = parse_browser_report(payload)
    if isinstance(parsed, BrowserReportError):
        return AcceptRefusal(parsed.reason)
    with transaction(conn):
        return _accept_pairs(conn, parsed.pairs, parsed.rejected, reporter, now)
