import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from subnet_tracker.core.db import transaction
from subnet_tracker.types import DomainId, ReconnectMode, RequestId, RequestKind, ResolveTrigger, Timestamp

_DOMAIN_TRIGGERS = (ResolveTrigger.NEW_DOMAINS, ResolveTrigger.MANUAL)
_WEAKEST_FIRST = (ReconnectMode.NONE, ReconnectMode.AUTO, ReconnectMode.ON_LEAVE, ReconnectMode.NOW)


@dataclass(frozen=True, slots=True)
class ResolveDomains:
    domain_ids: frozenset[DomainId]
    trigger: ResolveTrigger = ResolveTrigger.MANUAL

    def __post_init__(self) -> None:
        if self.trigger not in _DOMAIN_TRIGGERS:
            raise ValueError(f"domains are resolved on request only with trigger new_domains or manual, got {self.trigger}")


@dataclass(frozen=True, slots=True)
class ResolveAll:
    pass


@dataclass(frozen=True, slots=True)
class Apply:
    reconnect: ReconnectMode


type Request = ResolveDomains | ResolveAll | Apply


@dataclass(frozen=True, slots=True)
class TickWork:
    new_domains: frozenset[DomainId] = frozenset()
    # without the domains already in new_domains
    manual_domains: frozenset[DomainId] = frozenset()
    resolve_all: bool = False
    apply: ReconnectMode | None = None


# tick deletes the rows up to through once the work is done; new rows get larger ids while the row through exists
@dataclass(frozen=True, slots=True)
class Claim:
    through: RequestId
    work: TickWork


def encode_request(request: Request) -> tuple[RequestKind, str]:
    match request:
        case ResolveDomains(domain_ids, trigger):
            return RequestKind.RESOLVE_DOMAINS, json.dumps({"domain_ids": sorted(domain_ids), "trigger": trigger})
        case ResolveAll():
            return RequestKind.RESOLVE_ALL, "{}"
        case Apply(reconnect):
            return RequestKind.APPLY, json.dumps({"reconnect": reconnect})


def decode_request(kind: RequestKind, payload: str) -> Request:
    data = json.loads(payload)
    match kind:
        case RequestKind.RESOLVE_DOMAINS:
            return ResolveDomains(frozenset(DomainId(i) for i in data["domain_ids"]), ResolveTrigger(data["trigger"]))
        case RequestKind.RESOLVE_ALL:
            return ResolveAll()
        case RequestKind.APPLY:
            return Apply(ReconnectMode(data["reconnect"]))


def strongest_reconnect(modes: Iterable[ReconnectMode]) -> ReconnectMode | None:
    return max(modes, key=_WEAKEST_FIRST.index, default=None)


def merge_requests(requests: Sequence[Request]) -> TickWork:
    def domains(trigger: ResolveTrigger) -> frozenset[DomainId]:
        return frozenset(i for r in requests if isinstance(r, ResolveDomains) and r.trigger == trigger for i in r.domain_ids)

    new = domains(ResolveTrigger.NEW_DOMAINS)
    resolve_all = any(isinstance(r, ResolveAll) for r in requests)
    return TickWork(new, domains(ResolveTrigger.MANUAL) - new, resolve_all, strongest_reconnect(r.reconnect for r in requests if isinstance(r, Apply)))


def enqueue(conn: sqlite3.Connection, requests: Iterable[Request], now: Timestamp) -> tuple[RequestId, ...]:
    with transaction(conn):
        return tuple(RequestId(conn.execute("INSERT INTO requests (kind, payload, created_at) VALUES (?, ?, ?) RETURNING id", (*encode_request(r), now)).fetchone()["id"]) for r in requests)


def claim_requests(conn: sqlite3.Connection) -> Claim | None:
    rows = conn.execute("SELECT id, kind, payload FROM requests ORDER BY id").fetchall()
    if not rows:
        return None
    return Claim(RequestId(rows[-1]["id"]), merge_requests([decode_request(RequestKind(row["kind"]), row["payload"]) for row in rows]))


def finish_requests(conn: sqlite3.Connection, claim: Claim) -> int:
    return conn.execute("DELETE FROM requests WHERE id <= ?", (claim.through,)).rowcount
