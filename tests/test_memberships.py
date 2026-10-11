import pytest

from subnet_tracker.core.memberships import MembershipDiff, SetChange, membership_diff
from subnet_tracker.types import DomainId

D1, D2, D3 = DomainId(1), DomainId(2), DomainId(3)


@pytest.mark.parametrize(
    ("change", "current", "result"),
    [
        (SetChange[int](), {1, 2}, {1, 2}),
        (SetChange(add=frozenset({3})), {1}, {1, 3}),
        (SetChange(remove=frozenset({1, 9})), {1, 2}, {2}),
        (SetChange(replace=frozenset({7})), {1, 2}, {7}),
        (SetChange[int](replace=frozenset()), {1, 2}, set()),
        (SetChange(replace=frozenset({1, 2}), remove=frozenset({2})), {5}, {1}),
        (SetChange(add=frozenset({1}), remove=frozenset({1})), {2}, {1, 2}),
    ],
)
def test_change_replaces_then_removes_then_adds(change: SetChange[int], current: set[int], result: set[int]) -> None:
    assert change.applied(frozenset(current)) == result


def test_within_drops_unknown_ids_from_add_and_replace_only() -> None:
    change = SetChange(replace=frozenset({1, 9}), remove=frozenset({2, 8}), add=frozenset({3, 7}))
    assert change.within(frozenset({1, 2, 3})) == SetChange(replace=frozenset({1}), remove=frozenset({2, 8}), add=frozenset({3}))
    assert SetChange(add=frozenset({1})).within(frozenset()) == SetChange[int]()


def test_diff_lists_rows_to_add_and_remove_by_domain() -> None:
    current = {D1: frozenset({1}), D2: frozenset({1, 2})}
    diff = membership_diff(current, [D3, D2, D1], SetChange(remove=frozenset({2}), add=frozenset({1, 3})))
    assert diff == MembershipDiff(((D1, 3), (D2, 3), (D3, 1), (D3, 3)), ((D2, 2),))
    assert diff.owners == {D1, D2, D3}


def test_diff_skips_domains_whose_set_is_unchanged() -> None:
    diff = membership_diff({D1: frozenset({1})}, [D1, D2], SetChange(remove=frozenset({2}), add=frozenset({1})))
    assert diff == MembershipDiff(((D2, 1),), ())
    assert diff.owners == {D2}
