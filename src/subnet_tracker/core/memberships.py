import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass

from subnet_tracker.core.db import transaction
from subnet_tracker.types import DomainId, Timestamp

_IN = "IN (SELECT value FROM json_each(?))"


@dataclass(frozen=True, slots=True)
class SetChange[T: int]:
    replace: frozenset[T] | None = None
    remove: frozenset[T] = frozenset()
    add: frozenset[T] = frozenset()

    @property
    def targets(self) -> frozenset[T]:
        return self.add | (self.replace or frozenset())

    def applied(self, current: frozenset[T]) -> frozenset[T]:
        return ((current if self.replace is None else self.replace) - self.remove) | self.add

    def within(self, known: frozenset[T]) -> "SetChange[T]":
        return SetChange(None if self.replace is None else self.replace & known, self.remove, self.add & known)


@dataclass(frozen=True, slots=True)
class MembershipDiff[O: int, T: int]:
    added: tuple[tuple[O, T], ...]
    removed: tuple[tuple[O, T], ...]

    @property
    def owners(self) -> frozenset[O]:
        return frozenset(owner for owner, _ in (*self.added, *self.removed))


@dataclass(frozen=True, slots=True)
class Side[T: int]:
    table: str
    column: str  # the id of a table row in the link table
    make: Callable[[int], T]


DOMAIN = Side("domains", "domain_id", DomainId)


@dataclass(frozen=True, slots=True)
class Membership[O: int, T: int]:
    table: str
    owner: Side[O]  # its updated_at changes with its set
    target: Side[T]


def membership_diff[O: int, T: int](current: Mapping[O, frozenset[T]], owners: Iterable[O], change: SetChange[T]) -> MembershipDiff[O, T]:
    added: list[tuple[O, T]] = []
    removed: list[tuple[O, T]] = []
    for owner in sorted(owners):
        before = current.get(owner, frozenset())
        after = change.applied(before)
        added += ((owner, t) for t in sorted(after - before))
        removed += ((owner, t) for t in sorted(before - after))
    return MembershipDiff(tuple(added), tuple(removed))


def _ids(ids: Iterable[int]) -> str:
    return json.dumps(sorted(ids))


def _current[O: int, T: int](conn: sqlite3.Connection, m: Membership[O, T], owners: Collection[O]) -> dict[O, frozenset[T]]:
    current: defaultdict[O, set[T]] = defaultdict(set)
    for owner, target in conn.execute(f"SELECT {m.owner.column}, {m.target.column} FROM {m.table} WHERE {m.owner.column} {_IN}", (_ids(owners),)):
        current[m.owner.make(owner)].add(m.target.make(target))
    return {owner: frozenset(targets) for owner, targets in current.items()}


def edit_memberships[O: int, T: int](conn: sqlite3.Connection, m: Membership[O, T], owner_ids: Collection[O], change: SetChange[T], now: Timestamp) -> int:
    with transaction(conn):
        owners = [m.owner.make(i) for (i,) in conn.execute(f"SELECT id FROM {m.owner.table} WHERE id {_IN}", (_ids(owner_ids),))]
        known = frozenset(m.target.make(i) for (i,) in conn.execute(f"SELECT id FROM {m.target.table} WHERE id {_IN}", (_ids(change.targets),)))
        diff = membership_diff(_current(conn, m, owners), owners, change.within(known))
        conn.executemany(f"DELETE FROM {m.table} WHERE {m.owner.column} = ? AND {m.target.column} = ?", diff.removed)
        conn.executemany(f"INSERT INTO {m.table} ({m.owner.column}, {m.target.column}) VALUES (?, ?)", diff.added)
        conn.execute(f"UPDATE {m.owner.table} SET updated_at = ? WHERE id {_IN}", (now, _ids(diff.owners)))
    return len(diff.owners)
