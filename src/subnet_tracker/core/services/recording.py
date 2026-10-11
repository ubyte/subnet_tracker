import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from subnet_tracker.core.events import NewEvent, record_events
from subnet_tracker.core.requests import Apply, enqueue
from subnet_tracker.types import JsonValue, ReconnectMode, Timestamp

type Change = tuple[JsonValue, JsonValue]


@dataclass(frozen=True, slots=True)
class Recorded[T]:
    value: T
    applied: bool


def details_of(changes: Mapping[str, Change]) -> dict[str, JsonValue]:
    return {name: {"old": old, "new": new} for name, (old, new) in changes.items()}


def record_change(conn: sqlite3.Connection, events: Sequence[NewEvent], now: Timestamp, *, apply: bool) -> bool:
    record_events(conn, events, now)
    if apply:
        enqueue(conn, [Apply(ReconnectMode.AUTO)], now)
    return apply
