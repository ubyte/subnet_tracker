import pytest

from subnet_tracker.core.name_status import AddressState, NameStatus, ResolveResult, Sighting, address_state, domain_status, name_status
from subnet_tracker.types import ResolveStatus, Seconds, Timestamp

DAY = Seconds(86400)
S = ResolveStatus


def result(status: ResolveStatus, routes: bool = True) -> ResolveResult:
    answered = None if status in (S.ERROR, S.UNREACHABLE) else Timestamp(10)
    return ResolveResult("r", routes, status, Timestamp(10), answered, Timestamp(10) if status is S.OK else None, None)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), None),
        ((S.OK,), NameStatus.OK),
        ((S.ERROR, S.OK), NameStatus.OK),
        ((S.NXDOMAIN, S.OK), NameStatus.OK),
        ((S.NODATA,), NameStatus.NOTFOUND),
        ((S.NXDOMAIN, S.NOTFOUND, S.ERROR), NameStatus.NOTFOUND),
        ((S.ERROR, S.UNREACHABLE), NameStatus.ERROR),
    ],
)
def test_name_status_from_checked_resolvers(statuses: tuple[ResolveStatus, ...], expected: NameStatus | None) -> None:
    assert name_status([result(s) for s in statuses]) == expected


def test_unchecked_resolvers_do_not_count() -> None:
    assert name_status([result(S.OK, routes=False)]) is None
    assert name_status([result(S.OK, routes=False), result(S.NXDOMAIN)]) == NameStatus.NOTFOUND


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((None, None), None),
        ((None, NameStatus.ERROR), NameStatus.ERROR),
        ((NameStatus.ERROR, NameStatus.NOTFOUND), NameStatus.NOTFOUND),
        ((NameStatus.NOTFOUND, NameStatus.OK), NameStatus.OK),
        ((NameStatus.OK, None), NameStatus.OK),
    ],
)
def test_domain_status_is_the_best_of_its_names(statuses: tuple[NameStatus | None, ...], expected: NameStatus | None) -> None:
    assert domain_status(statuses) == expected


def sighting(last_seen: int, answered: int | None) -> Sighting:
    return Sighting("r", Timestamp(0), Timestamp(last_seen), None if answered is None else Timestamp(answered))


@pytest.mark.parametrize(
    ("sightings", "expected"),
    [
        ((sighting(100, 100),), AddressState.ACTIVE),
        ((sighting(100, 50),), AddressState.ACTIVE),
        ((sighting(100, None),), AddressState.ACTIVE),
        ((sighting(100, 100 + DAY - 1),), AddressState.STALE),
        ((sighting(100, 100 + DAY),), AddressState.EXPIRED),
        ((sighting(100, 100 + DAY), sighting(100, 101)), AddressState.STALE),
        ((sighting(100, 101), sighting(200, 200)), AddressState.ACTIVE),
    ],
)
def test_address_state_is_its_best_sighting(sightings: tuple[Sighting, ...], expected: AddressState) -> None:
    assert address_state(sightings, DAY) == expected
