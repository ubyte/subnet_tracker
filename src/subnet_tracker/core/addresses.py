import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from ipaddress import IPv4Network, IPv6Network, ip_address

from subnet_tracker.core.db import transaction
from subnet_tracker.types import AddressId, DomainId, DomainName, IPAddress, NameId, ResolverId, ResolverKind, ResolveStatus, Seconds, Timestamp

_ANSWERED = frozenset({ResolveStatus.OK, ResolveStatus.NODATA, ResolveStatus.NXDOMAIN, ResolveStatus.NOTFOUND})
_WITH_ERROR = frozenset({ResolveStatus.ERROR, ResolveStatus.UNREACHABLE})
_EXTERNAL = (ResolverKind.BROWSER, ResolverKind.AGENT)
_RANGE = (
    "SELECT a.id, a.ip, a.name_id, n.domain_id, n.name, s.resolver_id, s.first_seen_at, s.last_seen_at, nr.answered_at FROM addresses a"
    " JOIN names n ON n.id = a.name_id LEFT JOIN sightings s ON s.address_id = a.id"
    " LEFT JOIN name_resolves nr ON nr.name_id = a.name_id AND nr.resolver_id = s.resolver_id"
    " WHERE a.family = ? AND a.ip_bin BETWEEN ? AND ? ORDER BY a.ip_bin, n.name, a.id, s.resolver_id"
)


class SightingState(StrEnum):
    ACTIVE = auto()
    STALE = auto()
    EXPIRED = auto()


@dataclass(frozen=True, slots=True)
class Resolution:
    status: ResolveStatus
    addresses: frozenset[IPAddress] = frozenset()
    cnames: tuple[DomainName, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        # Addresses are the definition of ok; an error text belongs only to a failure, and it is optional there.
        if (self.status is ResolveStatus.OK) != bool(self.addresses):
            raise ValueError(f"status {self.status} {'requires' if self.status is ResolveStatus.OK else 'cannot carry'} addresses")
        if self.error is not None and self.status not in _WITH_ERROR:
            raise ValueError(f"status {self.status} cannot carry an error text")


@dataclass(frozen=True, slots=True)
class NameResolution:
    name_id: NameId
    resolver_id: ResolverId
    resolution: Resolution
    reporter: str | None = None


@dataclass(frozen=True, slots=True)
class NameResolve:
    name_id: NameId
    resolver_id: ResolverId
    attempted_at: Timestamp
    answered_at: Timestamp | None
    succeeded_at: Timestamp | None
    status: ResolveStatus
    error: str | None
    cnames: tuple[DomainName, ...]
    reporter: str | None
    requested_at: Timestamp | None


@dataclass(frozen=True, slots=True)
class AddressSighting:
    resolver_id: ResolverId
    first_seen_at: Timestamp
    last_seen_at: Timestamp
    # name_resolves.answered_at of this address's name and this resolver; none when that pair has no result
    answered_at: Timestamp | None


@dataclass(frozen=True, slots=True)
class StoredAddress:
    id: AddressId
    ip: IPAddress
    name_id: NameId
    domain_id: DomainId
    name: DomainName
    sightings: tuple[AddressSighting, ...]


@dataclass(frozen=True, slots=True)
class SharedIp:
    ip: IPAddress
    names: tuple[DomainName, ...]


def answer_times(status: ResolveStatus, now: Timestamp) -> tuple[Timestamp | None, Timestamp | None]:
    return (now if status in _ANSWERED else None, now if status is ResolveStatus.OK else None)


def sighting_state(sighting: AddressSighting, retention: Seconds) -> SightingState:
    if sighting.answered_at is None or sighting.last_seen_at >= sighting.answered_at:
        return SightingState.ACTIVE
    return SightingState.STALE if sighting.answered_at - sighting.last_seen_at < retention else SightingState.EXPIRED


def _cnames(raw: str | None) -> tuple[DomainName, ...]:
    if raw is None:
        return ()
    loaded = json.loads(raw)
    if not isinstance(loaded, list) or not all(isinstance(name, str) for name in loaded):
        raise ValueError(f"cnames must be a JSON list of names, got {raw!r}")
    return tuple(DomainName(name) for name in loaded)


def _time(value: int | None) -> Timestamp | None:
    return None if value is None else Timestamp(value)


def _name_resolve(row: sqlite3.Row) -> NameResolve:
    return NameResolve(
        NameId(row["name_id"]),
        ResolverId(row["resolver_id"]),
        Timestamp(row["attempted_at"]),
        _time(row["answered_at"]),
        _time(row["succeeded_at"]),
        ResolveStatus(row["status"]),
        row["error"],
        _cnames(row["cnames"]),
        row["reporter"],
        _time(row["requested_at"]),
    )


def name_resolves(conn: sqlite3.Connection, name_ids: Sequence[NameId]) -> tuple[NameResolve, ...]:
    if not name_ids:
        return ()
    marks = ", ".join("?" * len(name_ids))
    rows = conn.execute(f"SELECT * FROM name_resolves WHERE name_id IN ({marks}) ORDER BY name_id, resolver_id", tuple(name_ids))
    return tuple(_name_resolve(row) for row in rows)


def _sighting(row: sqlite3.Row) -> AddressSighting | None:
    if row["resolver_id"] is None:
        return None
    return AddressSighting(ResolverId(row["resolver_id"]), Timestamp(row["first_seen_at"]), Timestamp(row["last_seen_at"]), _time(row["answered_at"]))


def _stored(row: sqlite3.Row, sightings: tuple[AddressSighting, ...]) -> StoredAddress:
    return StoredAddress(AddressId(row["id"]), ip_address(row["ip"]), NameId(row["name_id"]), DomainId(row["domain_id"]), DomainName(row["name"]), sightings)


def addresses_in(conn: sqlite3.Connection, network: IPv4Network | IPv6Network) -> tuple[StoredAddress, ...]:
    groups: dict[int, tuple[sqlite3.Row, list[AddressSighting]]] = {}
    bounds = (network.version, network.network_address.packed, network.broadcast_address.packed)
    for row in conn.execute(_RANGE, bounds):
        if (bucket := groups.get(row["id"])) is None:
            bucket = (row, [])
            groups[row["id"]] = bucket
        if (sighting := _sighting(row)) is not None:
            bucket[1].append(sighting)
    return tuple(_stored(row, tuple(sightings)) for row, sightings in groups.values())


def shared_ips(addresses: Sequence[StoredAddress]) -> tuple[SharedIp, ...]:
    grouped: dict[IPAddress, list[DomainName]] = {}
    for address in addresses:
        grouped.setdefault(address.ip, []).append(address.name)
    return tuple(SharedIp(ip, tuple(names)) for ip, names in grouped.items())


# Existing rows only: a name without a result is already first in line. Browser and agent, not the server resolvers.
def request_external(conn: sqlite3.Connection, now: Timestamp, name_ids: Sequence[NameId] | None = None) -> int:
    if name_ids is not None and not name_ids:
        return 0
    sql = "UPDATE name_resolves SET requested_at = ? WHERE resolver_id IN (SELECT id FROM resolvers WHERE kind IN (?, ?))"
    args: list[object] = [now, *_EXTERNAL]
    if name_ids is not None:
        sql += f" AND name_id IN ({', '.join('?' * len(name_ids))})"
        args.extend(name_ids)
    with transaction(conn):
        return conn.execute(sql, args).rowcount
