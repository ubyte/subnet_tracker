from ipaddress import ip_address

import pytest
from hypothesis import given
from hypothesis import strategies as st

from subnet_tracker.resolver.model import NameResult, QueryResult, QueryType, combine
from subnet_tracker.types import DomainName, ResolveStatus

S = ResolveStatus
V4 = ip_address("192.0.2.1")
V6 = ip_address("2001:db8::1")
CDN = DomainName("cdn.example.net")
EDGE = DomainName("edge.example.net")
EDGE_V6 = DomainName("edge6.example.net")


def ok(*addresses: str, cnames: tuple[DomainName, ...] = ()) -> QueryResult:
    return QueryResult(S.OK, tuple(map(ip_address, addresses)), cnames)


def failed(status: ResolveStatus, error: str) -> QueryResult:
    return QueryResult(status, error=error)


NODATA = QueryResult(S.NODATA)
NXDOMAIN = QueryResult(S.NXDOMAIN)
NOTFOUND = QueryResult(S.NOTFOUND)
SERVFAIL = failed(S.ERROR, "SERVFAIL")
TIMEOUT = failed(S.ERROR, "timeout")
NO_ROUTE = failed(S.UNREACHABLE, "network is unreachable")


def test_query_types_carry_the_record_type_and_family() -> None:
    assert [(t.rdtype, t.version) for t in QueryType] == [(1, 4), (28, 6)]


@pytest.mark.parametrize(
    ("a", "aaaa", "expected"),
    [
        (ok("192.0.2.1"), ok("2001:db8::1"), NameResult(S.OK, (V4, V6))),
        (ok("192.0.2.1"), NODATA, NameResult(S.OK, (V4,))),
        (NODATA, ok("2001:db8::1"), NameResult(S.OK, (V6,))),
        (ok("192.0.2.1"), SERVFAIL, NameResult(S.OK, (V4,), error="AAAA: SERVFAIL")),
        (NO_ROUTE, ok("2001:db8::1"), NameResult(S.OK, (V6,), error="A: network is unreachable")),
        (NODATA, SERVFAIL, NameResult(S.ERROR, error="AAAA: SERVFAIL")),
        (NXDOMAIN, TIMEOUT, NameResult(S.ERROR, error="AAAA: timeout")),
        (SERVFAIL, TIMEOUT, NameResult(S.ERROR, error="A: SERVFAIL; AAAA: timeout")),
        (NO_ROUTE, NO_ROUTE, NameResult(S.UNREACHABLE, error="A: network is unreachable; AAAA: network is unreachable")),
        (NO_ROUTE, SERVFAIL, NameResult(S.ERROR, error="A: network is unreachable; AAAA: SERVFAIL")),
        (NO_ROUTE, NODATA, NameResult(S.ERROR, error="A: network is unreachable")),
        (NODATA, NODATA, NameResult(S.NODATA)),
        (NXDOMAIN, NXDOMAIN, NameResult(S.NXDOMAIN)),
        (NOTFOUND, NOTFOUND, NameResult(S.NOTFOUND)),
        (NXDOMAIN, NODATA, NameResult(S.NODATA)),
        (NODATA, NXDOMAIN, NameResult(S.NODATA)),
    ],
)
def test_combine_folds_a_and_aaaa_into_the_pair_status(a: QueryResult, aaaa: QueryResult, expected: NameResult) -> None:
    assert combine(a, aaaa) == expected


def test_combine_joins_cname_chains_in_chain_order_without_repeats() -> None:
    a = ok("192.0.2.1", cnames=(CDN, EDGE))
    aaaa = ok("2001:db8::1", cnames=(CDN, EDGE_V6))
    assert combine(a, aaaa).cnames == (CDN, EDGE, EDGE_V6)


def test_combine_keeps_the_chain_of_a_negative_answer() -> None:
    assert combine(QueryResult(S.NODATA, cnames=(CDN,)), QueryResult(S.NODATA, cnames=(CDN,))) == NameResult(S.NODATA, cnames=(CDN,))


@pytest.mark.parametrize("cls", [QueryResult, NameResult])
def test_ok_is_exactly_a_result_with_addresses(cls: type[QueryResult | NameResult]) -> None:
    with pytest.raises(ValueError, match="addresses"):
        cls(S.OK)
    with pytest.raises(ValueError, match="addresses"):
        cls(S.NODATA, (V4,))


_queries = st.one_of(
    st.lists(st.ip_addresses(), min_size=1, max_size=3, unique=True).map(lambda ips: QueryResult(S.OK, tuple(ips))),
    st.sampled_from([NODATA, NXDOMAIN, NOTFOUND, SERVFAIL, TIMEOUT, NO_ROUTE]),
)


@given(_queries, _queries)
def test_combined_status_does_not_depend_on_the_order_of_queries(a: QueryResult, b: QueryResult) -> None:
    assert combine(a, b).status == combine(b, a).status


@given(_queries, _queries)
def test_combined_result_is_ok_exactly_when_a_query_has_addresses_and_keeps_them_all(a: QueryResult, aaaa: QueryResult) -> None:
    result = combine(a, aaaa)
    assert (result.status is S.OK) == bool(a.addresses or aaaa.addresses)
    assert set(result.addresses) == {*a.addresses, *aaaa.addresses}
