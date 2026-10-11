from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum, auto

from subnet_tracker.types import ResolveStatus, Seconds, Timestamp

_NOT_FOUND = frozenset({ResolveStatus.NODATA, ResolveStatus.NXDOMAIN, ResolveStatus.NOTFOUND})


class NameStatus(StrEnum):
    OK = auto()
    NOTFOUND = auto()
    ERROR = auto()


class AddressState(StrEnum):
    ACTIVE = auto()
    STALE = auto()
    EXPIRED = auto()


_BEST_FIRST = tuple(NameStatus)
_FRESHEST_FIRST = tuple(AddressState)


@dataclass(frozen=True, slots=True)
class ResolveResult:
    resolver: str
    # a checked resolver: used for routes and, for an agent, not revoked
    routes: bool
    status: ResolveStatus
    attempted_at: Timestamp
    answered_at: Timestamp | None
    succeeded_at: Timestamp | None
    error: str | None


@dataclass(frozen=True, slots=True)
class Sighting:
    resolver: str
    first_seen_at: Timestamp
    last_seen_at: Timestamp
    # name_resolves.answered_at of the address's name and this resolver
    answered_at: Timestamp | None


def name_status(results: Iterable[ResolveResult]) -> NameStatus | None:
    checked = {r.status for r in results if r.routes}
    if not checked:
        return None
    if ResolveStatus.OK in checked:
        return NameStatus.OK
    return NameStatus.NOTFOUND if checked & _NOT_FOUND else NameStatus.ERROR


def domain_status(statuses: Iterable[NameStatus | None]) -> NameStatus | None:
    return min((s for s in statuses if s is not None), key=_BEST_FIRST.index, default=None)


def sighting_state(sighting: Sighting, retention: Seconds) -> AddressState:
    if sighting.answered_at is None or sighting.last_seen_at >= sighting.answered_at:
        return AddressState.ACTIVE
    return AddressState.STALE if sighting.answered_at - sighting.last_seen_at < retention else AddressState.EXPIRED


def address_state(sightings: Iterable[Sighting], retention: Seconds) -> AddressState:
    return min((sighting_state(s, retention) for s in sightings), key=_FRESHEST_FIRST.index, default=AddressState.EXPIRED)
