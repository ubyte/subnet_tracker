import re
import sqlite3
from enum import StrEnum

import pytest

from subnet_tracker.types import AgentMode, ApplyStatus, ApplyTrigger, BatchKind, DomainSource, PendingReconnect, ReconnectMode, RequestKind, ResolverKind, ResolveStatus, ResolveTrigger, RouteKind

ENUMS: dict[tuple[str, str], type[StrEnum]] = {
    ("add_batches", "kind"): BatchKind,
    ("domains", "source"): DomainSource,
    ("resolvers", "kind"): ResolverKind,
    ("agents", "mode"): AgentMode,
    ("name_resolves", "status"): ResolveStatus,
    ("plan_routes", "kind"): RouteKind,
    ("reconnect_state", "pending_mode"): PendingReconnect,
    ("requests", "kind"): RequestKind,
    ("resolve_runs", "trigger"): ResolveTrigger,
    ("apply_runs", "trigger"): ApplyTrigger,
    ("apply_runs", "reconnect"): ReconnectMode,
    ("apply_runs", "status"): ApplyStatus,
}

ENUM_CHECK = re.compile(r"^\s*(\w+)\s+TEXT\b[^\n]*?CHECK \(\1 IN \(([^)]*)\)\)", re.MULTILINE)


def enum_checks(db: sqlite3.Connection) -> dict[tuple[str, str], set[str]]:
    rows = db.execute("SELECT name, sql FROM sqlite_schema WHERE type = 'table'")
    return {(row["name"], column): {v.strip().strip("'") for v in values.split(",")} for row in rows for column, values in ENUM_CHECK.findall(row["sql"])}


def test_every_enumerated_check_has_str_enum(db: sqlite3.Connection) -> None:
    assert enum_checks(db).keys() == ENUMS.keys()


@pytest.mark.parametrize(("table", "column"), ENUMS, ids=[f"{t}.{c}" for t, c in ENUMS])
def test_str_enum_matches_check(db: sqlite3.Connection, table: str, column: str) -> None:
    assert {member.value for member in ENUMS[table, column]} == enum_checks(db)[table, column]
