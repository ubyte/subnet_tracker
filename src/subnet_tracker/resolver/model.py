from dataclasses import dataclass
from enum import StrEnum

from subnet_tracker.types import DomainName, IPAddress, ResolveStatus, Seconds


class QueryType(StrEnum):
    A = "A"
    AAAA = "AAAA"

    @property
    def rdtype(self) -> int:
        return 1 if self is QueryType.A else 28

    @property
    def version(self) -> int:
        return 4 if self is QueryType.A else 6


def _check_addresses(status: ResolveStatus, addresses: tuple[IPAddress, ...]) -> None:
    if (status is ResolveStatus.OK) != bool(addresses):
        raise ValueError("ok is exactly a result with addresses")


@dataclass(frozen=True, slots=True)
class QueryResult:
    status: ResolveStatus
    addresses: tuple[IPAddress, ...] = ()
    cnames: tuple[DomainName, ...] = ()
    ttl: Seconds | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _check_addresses(self.status, self.addresses)


@dataclass(frozen=True, slots=True)
class NameResult:
    status: ResolveStatus
    addresses: tuple[IPAddress, ...] = ()
    cnames: tuple[DomainName, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        _check_addresses(self.status, self.addresses)


_NEGATIVE = frozenset({ResolveStatus.NODATA, ResolveStatus.NXDOMAIN, ResolveStatus.NOTFOUND})


def _failure(kind: QueryType, result: QueryResult) -> str | None:
    if result.status not in (ResolveStatus.ERROR, ResolveStatus.UNREACHABLE):
        return None
    return f"{kind}: {result.error}" if result.error else str(kind)


def _unique(names: tuple[DomainName, ...], extra: tuple[DomainName, ...]) -> tuple[DomainName, ...]:
    seen: set[DomainName] = set()
    ordered: list[DomainName] = []
    for name in (*names, *extra):
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return tuple(ordered)


def combine(a: QueryResult, aaaa: QueryResult) -> NameResult:
    error = "; ".join(text for text in (_failure(QueryType.A, a), _failure(QueryType.AAAA, aaaa)) if text) or None
    cnames = _unique(a.cnames, aaaa.cnames)
    addresses = (*a.addresses, *aaaa.addresses)
    if addresses:
        return NameResult(ResolveStatus.OK, addresses, cnames, error)
    if a.status in _NEGATIVE and aaaa.status in _NEGATIVE:
        # NOERROR on either side means the name exists, so two different negatives are nodata
        status = a.status if a.status is aaaa.status else ResolveStatus.NODATA
        return NameResult(status, cnames=cnames)
    # a failure without an authoritative answer is unreachable only when both queries are
    status = ResolveStatus.UNREACHABLE if a.status is ResolveStatus.UNREACHABLE and aaaa.status is ResolveStatus.UNREACHABLE else ResolveStatus.ERROR
    return NameResult(status, cnames=cnames, error=error)
