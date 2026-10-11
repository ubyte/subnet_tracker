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
class MembershipDiff[T: int]:
    added: tuple[tuple[DomainId, T], ...]
    removed: tuple[tuple[DomainId, T], ...]

    @property
    def domains(self) -> frozenset[DomainId]:
        return frozenset(domain for domain, _ in (*self.added, *self.removed))


@dataclass(frozen=True, slots=True)
class Membership[T: int]:
    table: str
    column: str
    targets: str
    make: Callable[[int], T]


def membership_diff[T: int](current: Mapping[DomainId, frozenset[T]], domains: Iterable[DomainId], change: SetChange[T]) -> MembershipDiff[T]:
    added: list[tuple[DomainId, T]] = []
    removed: list[tuple[DomainId, T]] = []
    for domain in sorted(domains):
        before = current.get(domain, frozenset())
        after = change.applied(before)
        added += ((domain, t) for t in sorted(after - before))
        removed += ((domain, t) for t in sorted(before - after))
    return MembershipDiff(tuple(added), tuple(removed))


def _ids(ids: Iterable[int]) -> str:
    return json.dumps(sorted(ids))


def _current[T: int](conn: sqlite3.Connection, m: Membership[T], domains: Collection[DomainId]) -> dict[DomainId, frozenset[T]]:
    current: defaultdict[DomainId, set[T]] = defaultdict(set)
    for domain_id, target in conn.execute(f"SELECT domain_id, {m.column} FROM {m.table} WHERE domain_id {_IN}", (_ids(domains),)):
        current[DomainId(domain_id)].add(m.make(target))
    return {domain: frozenset(targets) for domain, targets in current.items()}


def edit_memberships[T: int](conn: sqlite3.Connection, m: Membership[T], domain_ids: Collection[DomainId], change: SetChange[T], now: Timestamp) -> int:
    with transaction(conn):
        domains = [DomainId(i) for (i,) in conn.execute(f"SELECT id FROM domains WHERE id {_IN}", (_ids(domain_ids),))]
        known = frozenset(m.make(i) for (i,) in conn.execute(f"SELECT id FROM {m.targets} WHERE id {_IN}", (_ids(change.targets),)))
        diff = membership_diff(_current(conn, m, domains), domains, change.within(known))
        conn.executemany(f"DELETE FROM {m.table} WHERE domain_id = ? AND {m.column} = ?", diff.removed)
        conn.executemany(f"INSERT INTO {m.table} (domain_id, {m.column}) VALUES (?, ?)", diff.added)
        conn.execute(f"UPDATE domains SET updated_at = ? WHERE id {_IN}", (now, _ids(diff.domains)))
    return len(diff.domains)
