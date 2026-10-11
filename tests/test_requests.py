import json
import sqlite3

import pytest

from domains_world import NOW, count, paste_and_fail
from subnet_tracker.core.db import transaction
from subnet_tracker.core.requests import (
    Apply,
    Claim,
    Request,
    ResolveAll,
    ResolveDomains,
    TickWork,
    claim_requests,
    decode_request,
    encode_request,
    enqueue,
    finish_requests,
    merge_requests,
    strongest_reconnect,
)
from subnet_tracker.types import DomainId, ReconnectMode, RequestId, RequestKind, ResolveTrigger

NEW = ResolveTrigger.NEW_DOMAINS
NONE, AUTO, ON_LEAVE, NOW_MODE = ReconnectMode.NONE, ReconnectMode.AUTO, ReconnectMode.ON_LEAVE, ReconnectMode.NOW


def ids(*values: int) -> frozenset[DomainId]:
    return frozenset(DomainId(v) for v in values)


@pytest.mark.parametrize(
    ("request_", "kind", "payload"),
    [
        (ResolveDomains(ids(3, 1, 2), NEW), RequestKind.RESOLVE_DOMAINS, {"domain_ids": [1, 2, 3], "trigger": "new_domains"}),
        (ResolveDomains(ids(5)), RequestKind.RESOLVE_DOMAINS, {"domain_ids": [5], "trigger": "manual"}),
        (ResolveAll(), RequestKind.RESOLVE_ALL, {}),
        (Apply(ON_LEAVE), RequestKind.APPLY, {"reconnect": "on_leave"}),
        (Apply(NONE), RequestKind.APPLY, {"reconnect": "none"}),
    ],
)
def test_encoding_round_trip(request_: Request, kind: RequestKind, payload: dict[str, object]) -> None:
    encoded_kind, encoded = encode_request(request_)
    assert (encoded_kind, json.loads(encoded)) == (kind, payload)
    assert decode_request(kind, encoded) == request_


@pytest.mark.parametrize("trigger", [ResolveTrigger.TIMER, ResolveTrigger.BROWSER, ResolveTrigger.AGENT])
def test_domains_are_resolved_on_request_only_as_new_or_manual(trigger: ResolveTrigger) -> None:
    with pytest.raises(ValueError, match="new_domains or manual"):
        ResolveDomains(ids(1), trigger)


@pytest.mark.parametrize(
    ("modes", "strongest"),
    [
        ((), None),
        ((NONE,), NONE),
        ((NONE, AUTO), AUTO),
        ((ON_LEAVE, AUTO), ON_LEAVE),
        ((AUTO, NOW_MODE, ON_LEAVE, NONE), NOW_MODE),
    ],
)
def test_strongest_reconnect(modes: tuple[ReconnectMode, ...], strongest: ReconnectMode | None) -> None:
    assert strongest_reconnect(modes) == strongest
    assert strongest_reconnect(reversed(modes)) == strongest


def test_merge_nothing() -> None:
    assert merge_requests([]) == TickWork()


def test_merge_unites_domains_per_trigger_and_drops_manual_ones_already_new() -> None:
    requests = [ResolveDomains(ids(1, 2), NEW), ResolveDomains(ids(2, 3)), ResolveDomains(ids(4), NEW), ResolveDomains(ids(5, 3))]
    assert merge_requests(requests) == TickWork(new_domains=ids(1, 2, 4), manual_domains=ids(3, 5))


def test_merge_keeps_resolve_all_and_the_strongest_apply() -> None:
    requests: list[Request] = [Apply(AUTO), ResolveAll(), ResolveDomains(ids(1), NEW), Apply(ON_LEAVE), ResolveAll(), Apply(NONE)]
    assert merge_requests(requests) == TickWork(new_domains=ids(1), resolve_all=True, apply=ON_LEAVE)


def test_empty_queue_claims_nothing(db: sqlite3.Connection) -> None:
    assert claim_requests(db) is None


def test_enqueue_and_claim(db: sqlite3.Connection) -> None:
    first = enqueue(db, [ResolveDomains(ids(1), NEW), Apply(ON_LEAVE)], NOW)
    second = enqueue(db, [Apply(NOW_MODE)], NOW)
    assert (first, second) == ((RequestId(1), RequestId(2)), (RequestId(3),))
    assert claim_requests(db) == Claim(RequestId(3), TickWork(new_domains=ids(1), apply=NOW_MODE))
    assert [tuple(row) for row in db.execute("SELECT kind, created_at FROM requests ORDER BY id")] == [("resolve_domains", NOW), ("apply", NOW), ("apply", NOW)]


def test_claim_does_not_remove_rows(db: sqlite3.Connection) -> None:
    enqueue(db, [ResolveAll()], NOW)
    assert claim_requests(db) == claim_requests(db) == Claim(RequestId(1), TickWork(resolve_all=True))


def test_finish_removes_only_claimed_rows(db: sqlite3.Connection) -> None:
    enqueue(db, [ResolveAll(), Apply(AUTO)], NOW)
    claim = claim_requests(db)
    assert claim is not None
    enqueue(db, [Apply(NOW_MODE)], NOW)
    assert finish_requests(db, claim) == 2
    assert claim_requests(db) == Claim(RequestId(3), TickWork(apply=NOW_MODE))


def test_requests_roll_back_with_the_change(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        paste_and_fail(db, "example.com", lambda: enqueue(db, [ResolveDomains(ids(1), NEW), Apply(ON_LEAVE)], NOW))
    assert (count(db, "domains"), count(db, "requests")) == (0, 0)


def test_enqueue_nests_in_the_outer_transaction(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    with transaction(db):
        enqueue(db, [Apply(AUTO)], NOW)
        assert claim_requests(other) is None
    assert claim_requests(other) == Claim(RequestId(1), TickWork(apply=AUTO))
