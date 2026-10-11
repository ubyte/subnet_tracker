import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace

from subnet_tracker.core.addresses import NameResolution
from subnet_tracker.core.db import transaction
from subnet_tracker.core.services.resolves import Recorded, record_resolutions
from subnet_tracker.types import NameId, ResolverId, ResolveRunId, ResolveTrigger, Seconds, Timestamp

# History cleanup runs once a day. How old a row must be is the caller's cutoff.
CLEANUP_INTERVAL = Seconds(86400)
_EXTERNAL = frozenset({ResolveTrigger.BROWSER, ResolveTrigger.AGENT})
_TIMING = "a resolver reports timing on every portion of the run or on none"
_SCHEDULER = """
INSERT INTO scheduler_state (id, full_run_id, full_cursor_name_id, last_full_started_at, last_cleanup_at) VALUES (1, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
  full_run_id = excluded.full_run_id, full_cursor_name_id = excluded.full_cursor_name_id,
  last_full_started_at = excluded.last_full_started_at, last_cleanup_at = excluded.last_cleanup_at
"""
_INSERT_RUN = "INSERT INTO resolve_runs (trigger, resolver_id, started_at, domains_total) VALUES (?, ?, ?, ?) RETURNING id"
_SELECT_RUN = "SELECT id, trigger, resolver_id, started_at, finished_at, domains_total, addresses_new, addresses_expired, error FROM resolve_runs"
_UPSERT_STATS = """
INSERT INTO resolve_run_resolvers (run_id, resolver_id, queries, ok, notfound, errors, unreachable, avg_ms, healthy)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (run_id, resolver_id) DO UPDATE SET
  queries = excluded.queries, ok = excluded.ok, notfound = excluded.notfound, errors = excluded.errors,
  unreachable = excluded.unreachable, avg_ms = excluded.avg_ms, healthy = excluded.healthy
"""


@dataclass(frozen=True, slots=True)
class SchedulerState:
    full_run_id: ResolveRunId | None = None
    full_cursor_name_id: NameId | None = None
    last_full_started_at: Timestamp | None = None
    last_cleanup_at: Timestamp | None = None


@dataclass(frozen=True, slots=True)
class NewResolveRun:
    trigger: ResolveTrigger
    started_at: Timestamp
    domains_total: int = 0
    resolver_id: ResolverId | None = None

    def __post_init__(self) -> None:
        if type(self.domains_total) is not int or self.domains_total < 0:
            raise ValueError(f"domains_total must be >= 0, got {self.domains_total!r}")
        external = self.trigger in _EXTERNAL
        if external and self.resolver_id is None:
            raise ValueError("browser and agent runs name their resolver")
        if not external and self.resolver_id is not None:
            raise ValueError("only browser and agent runs name a resolver")


@dataclass(frozen=True, slots=True)
class ResolveRun:
    id: ResolveRunId
    trigger: ResolveTrigger
    resolver_id: ResolverId | None
    started_at: Timestamp
    finished_at: Timestamp | None
    domains_total: int
    addresses_new: int
    addresses_expired: int
    error: str | None


@dataclass(frozen=True, slots=True)
class ResolverPortion:
    resolver_id: ResolverId
    queries: int
    ok: int
    notfound: int
    errors: int
    unreachable: int
    total_ms: int | None = None
    healthy: bool | None = None

    def __post_init__(self) -> None:
        for name in ("queries", "ok", "notfound", "errors", "unreachable"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be an integer >= 0, got {value!r}")
        if self.total_ms is not None and (type(self.total_ms) is not int or self.total_ms < 0):
            raise ValueError(f"total_ms must be an integer >= 0, got {self.total_ms!r}")
        if self.total_ms is not None and self.queries == 0:
            raise ValueError("timing needs queries to weight the average")


@dataclass(frozen=True, slots=True)
class ResolverRunStats:
    resolver_id: ResolverId
    queries: int
    ok: int
    notfound: int
    errors: int
    unreachable: int
    avg_ms: int | None
    healthy: bool


def new_full_pass_due(state: SchedulerState, interval: Seconds, now: Timestamp) -> bool:
    # An open pass is continued, not opened again. A new one waits out resolve.interval; the boundary is due.
    if state.full_run_id is not None:
        return False
    started = state.last_full_started_at
    return started is None or now - started >= interval


def cleanup_due(last_cleanup_at: Timestamp | None, now: Timestamp) -> bool:
    return last_cleanup_at is None or now - last_cleanup_at >= CLEANUP_INTERVAL


def _time(value: int | None) -> Timestamp | None:
    return None if value is None else Timestamp(value)


def _run_id(value: int | None) -> ResolveRunId | None:
    return None if value is None else ResolveRunId(value)


def _name_id(value: int | None) -> NameId | None:
    return None if value is None else NameId(value)


def _resolver_id(value: int | None) -> ResolverId | None:
    return None if value is None else ResolverId(value)


def _missing(run_id: ResolveRunId) -> ValueError:
    return ValueError(f"resolve run {run_id} does not exist")


def load_scheduler_state(conn: sqlite3.Connection) -> SchedulerState:
    row = conn.execute("SELECT full_run_id, full_cursor_name_id, last_full_started_at, last_cleanup_at FROM scheduler_state WHERE id = 1").fetchone()
    if row is None:
        return SchedulerState()
    return SchedulerState(_run_id(row["full_run_id"]), _name_id(row["full_cursor_name_id"]), _time(row["last_full_started_at"]), _time(row["last_cleanup_at"]))


def save_scheduler_state(conn: sqlite3.Connection, state: SchedulerState) -> None:
    with transaction(conn):
        conn.execute(_SCHEDULER, (state.full_run_id, state.full_cursor_name_id, state.last_full_started_at, state.last_cleanup_at))


def _resolve_run(row: sqlite3.Row) -> ResolveRun:
    return ResolveRun(
        ResolveRunId(row["id"]),
        ResolveTrigger(row["trigger"]),
        _resolver_id(row["resolver_id"]),
        Timestamp(row["started_at"]),
        _time(row["finished_at"]),
        row["domains_total"],
        row["addresses_new"],
        row["addresses_expired"],
        row["error"],
    )


def start_resolve_run(conn: sqlite3.Connection, run: NewResolveRun) -> ResolveRunId:
    with transaction(conn):
        row = conn.execute(_INSERT_RUN, (run.trigger, run.resolver_id, run.started_at, run.domains_total)).fetchone()
        return ResolveRunId(row["id"])


def _require_run(conn: sqlite3.Connection, run_id: ResolveRunId) -> None:
    if conn.execute("SELECT 1 FROM resolve_runs WHERE id = ?", (run_id,)).fetchone() is None:
        raise _missing(run_id)


def finish_resolve_run(conn: sqlite3.Connection, run_id: ResolveRunId, finished_at: Timestamp, error: str | None = None) -> None:
    with transaction(conn):
        if conn.execute("UPDATE resolve_runs SET finished_at = ?, error = ? WHERE id = ?", (finished_at, error, run_id)).rowcount == 0:
            raise _missing(run_id)


def add_addresses_expired(conn: sqlite3.Connection, run_id: ResolveRunId, count: int) -> None:
    if type(count) is not int or count < 0:
        raise ValueError(f"count must be an integer >= 0, got {count!r}")
    with transaction(conn):
        if conn.execute("UPDATE resolve_runs SET addresses_expired = addresses_expired + ? WHERE id = ?", (count, run_id)).rowcount == 0:
            raise _missing(run_id)


def open_full_pass(conn: sqlite3.Connection, now: Timestamp, domains_total: int) -> ResolveRunId:
    with transaction(conn):
        state = load_scheduler_state(conn)
        if state.full_run_id is not None:
            raise ValueError("a full resolve pass is already open")
        run_id = start_resolve_run(conn, NewResolveRun(ResolveTrigger.TIMER, now, domains_total))
        save_scheduler_state(conn, SchedulerState(run_id, None, now, state.last_cleanup_at))
        return run_id


def advance_full_pass(conn: sqlite3.Connection, name_id: NameId) -> None:
    with transaction(conn):
        state = load_scheduler_state(conn)
        if state.full_run_id is None:
            raise ValueError("no full resolve pass is open")
        save_scheduler_state(conn, replace(state, full_cursor_name_id=name_id))


def close_full_pass(conn: sqlite3.Connection, now: Timestamp, error: str | None = None) -> None:
    # The cursor belongs to the open pass. The start time stays, so the next pass waits out the interval.
    with transaction(conn):
        state = load_scheduler_state(conn)
        if state.full_run_id is None:
            raise ValueError("no full resolve pass is open")
        finish_resolve_run(conn, state.full_run_id, now, error)
        save_scheduler_state(conn, SchedulerState(None, None, state.last_full_started_at, state.last_cleanup_at))


def mark_cleanup(conn: sqlite3.Connection, now: Timestamp) -> None:
    with transaction(conn):
        save_scheduler_state(conn, replace(load_scheduler_state(conn), last_cleanup_at=now))


def list_resolve_runs(conn: sqlite3.Connection, limit: int) -> tuple[ResolveRun, ...]:
    return tuple(_resolve_run(row) for row in conn.execute(f"{_SELECT_RUN} ORDER BY id DESC LIMIT ?", (limit,)))


def run_resolvers(conn: sqlite3.Connection, run_id: ResolveRunId) -> tuple[ResolverRunStats, ...]:
    rows = conn.execute(
        "SELECT resolver_id, queries, ok, notfound, errors, unreachable, avg_ms, healthy FROM resolve_run_resolvers WHERE run_id = ? ORDER BY resolver_id",
        (run_id,),
    )
    return tuple(ResolverRunStats(ResolverId(row["resolver_id"]), row["queries"], row["ok"], row["notfound"], row["errors"], row["unreachable"], row["avg_ms"], bool(row["healthy"])) for row in rows)


def _avg(previous: ResolverRunStats | None, portion: ResolverPortion) -> int | None:
    # Untimed portions leave the mean. Once any portion reports a sum, every portion of that resolver does, so avg_ms * queries is the timed sum.
    if portion.total_ms is None:
        if previous is not None and previous.avg_ms is not None:
            raise ValueError(_TIMING)
        return None if previous is None else previous.avg_ms
    if previous is not None and previous.queries > 0 and previous.avg_ms is None:
        raise ValueError(_TIMING)
    if previous is None or previous.avg_ms is None:
        return portion.total_ms // portion.queries
    return (previous.avg_ms * previous.queries + portion.total_ms) // (previous.queries + portion.queries)


def _merged(previous: ResolverRunStats | None, portion: ResolverPortion) -> ResolverRunStats:
    queries = portion.queries if previous is None else previous.queries + portion.queries
    ok = portion.ok if previous is None else previous.ok + portion.ok
    notfound = portion.notfound if previous is None else previous.notfound + portion.notfound
    errors = portion.errors if previous is None else previous.errors + portion.errors
    unreachable = portion.unreachable if previous is None else previous.unreachable + portion.unreachable
    healthy = portion.healthy if portion.healthy is not None else (True if previous is None else previous.healthy)
    return ResolverRunStats(portion.resolver_id, queries, ok, notfound, errors, unreachable, _avg(previous, portion), healthy)


def _merge_portions(conn: sqlite3.Connection, run_id: ResolveRunId, portions: Sequence[ResolverPortion]) -> dict[ResolverId, ResolverRunStats]:
    current = {stats.resolver_id: stats for stats in run_resolvers(conn, run_id)}
    for portion in portions:
        current[portion.resolver_id] = _merged(current.get(portion.resolver_id), portion)
    return {portion.resolver_id: current[portion.resolver_id] for portion in portions}


def record_run_results(conn: sqlite3.Connection, run_id: ResolveRunId, results: Sequence[NameResolution], portions: Sequence[ResolverPortion], actor: str, now: Timestamp) -> Recorded:
    with transaction(conn):
        _require_run(conn, run_id)
        merged = _merge_portions(conn, run_id, portions)
        recorded = record_resolutions(conn, results, actor, now)
        conn.execute("UPDATE resolve_runs SET addresses_new = addresses_new + ? WHERE id = ?", (recorded.addresses_new, run_id))
        conn.executemany(
            _UPSERT_STATS,
            ((run_id, stats.resolver_id, stats.queries, stats.ok, stats.notfound, stats.errors, stats.unreachable, stats.avg_ms, int(stats.healthy)) for stats in merged.values()),
        )
        return recorded


def prune_resolve_runs(conn: sqlite3.Connection, cutoff: Timestamp) -> int:
    # started_at at the cutoff stays, as with events. Deleting the open pass clears full_run_id by ON DELETE SET NULL and leaves the cursor.
    with transaction(conn):
        return conn.execute("DELETE FROM resolve_runs WHERE started_at < ?", (cutoff,)).rowcount
