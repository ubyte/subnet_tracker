import itertools
from ipaddress import ip_address

import pytest
from hypothesis import given

from doh_samples import (
    AAAA,
    CLOUDFLARE_A,
    CLOUDFLARE_AAAA_NODATA,
    CLOUDFLARE_CNAME_A,
    CLOUDFLARE_NXDOMAIN,
    CNAME,
    GOOGLE_A,
    GOOGLE_CNAME_AAAA,
    GOOGLE_HTTP_400,
    GOOGLE_NXDOMAIN,
    GOOGLE_SERVFAIL,
    A,
    body,
    record,
)
from doh_strategies import json_values
from subnet_tracker.resolver.doh_json import MAX_ADDRESSES, MAX_CHAIN, MAX_RECORDS, DohError, DohProblem, classify_doh, parse_doh_json
from subnet_tracker.resolver.model import QueryResult, QueryType
from subnet_tracker.types import DomainName, IPAddress, JsonValue, ResolveStatus, Seconds

S = ResolveStatus
P = DohProblem
NAME = DomainName("example.com")
YOUTUBE_UI = DomainName("youtube-ui.l.google.com")


def ips(*texts: str) -> tuple[IPAddress, ...]:
    return tuple(map(ip_address, texts))


def parse(answer: list[JsonValue], qtype: QueryType = QueryType.A, status: JsonValue = 0) -> QueryResult | DohError:
    return parse_doh_json(NAME, qtype, body(NAME, qtype.rdtype, answer, status))


def problem(result: QueryResult | DohError) -> DohProblem | None:
    return result.problem if isinstance(result, DohError) else None


@pytest.mark.parametrize(
    ("name", "qtype", "sample", "expected"),
    [
        ("example.com", QueryType.A, CLOUDFLARE_A, QueryResult(S.OK, ips("93.184.215.14"), ttl=Seconds(3241))),
        ("www.youtube.com", QueryType.A, CLOUDFLARE_CNAME_A, QueryResult(S.OK, ips("142.250.74.14", "142.250.74.46"), (YOUTUBE_UI,), Seconds(187))),
        ("ipv4only.arpa", QueryType.AAAA, CLOUDFLARE_AAAA_NODATA, QueryResult(S.NODATA)),
        ("nonexistent.example.com", QueryType.A, CLOUDFLARE_NXDOMAIN, QueryResult(S.NXDOMAIN)),
        ("example.com", QueryType.A, GOOGLE_A, QueryResult(S.OK, ips("93.184.215.14"), ttl=Seconds(3600))),
        ("www.youtube.com", QueryType.AAAA, GOOGLE_CNAME_AAAA, QueryResult(S.OK, ips("2a00:1450:4001:80b::200e", "2a00:1450:4001:810::200e"), (YOUTUBE_UI,), Seconds(300))),
        ("nonexistent.example.com", QueryType.AAAA, GOOGLE_NXDOMAIN, QueryResult(S.NXDOMAIN)),
        ("dnssec-failed.org", QueryType.A, GOOGLE_SERVFAIL, QueryResult(S.ERROR, error="SERVFAIL")),
    ],
)
def test_provider_answers_are_classified(name: str, qtype: QueryType, sample: JsonValue, expected: QueryResult) -> None:
    assert parse_doh_json(DomainName(name), qtype, sample) == expected
    assert classify_doh(DomainName(name), qtype, 200, None, sample) == expected


@pytest.mark.parametrize(("status", "text"), [(1, "FORMERR"), (2, "SERVFAIL"), (4, "NOTIMP"), (5, "REFUSED"), (23, "BADCOOKIE"), (4095, "4095")])
def test_other_response_codes_are_errors_named_by_the_code(status: int, text: str) -> None:
    assert parse([], status=status) == QueryResult(S.ERROR, error=text)


def test_an_error_code_does_not_look_at_the_answer() -> None:
    assert parse(["garbage"], status=5) == QueryResult(S.ERROR, error="REFUSED")


@pytest.mark.parametrize(("http_status", "sample"), [(400, GOOGLE_HTTP_400), (500, None), (503, "Service Unavailable"), (301, CLOUDFLARE_A)])
def test_http_statuses_other_than_200_are_errors(http_status: int, sample: JsonValue) -> None:
    assert classify_doh(NAME, QueryType.A, http_status, None, sample) == QueryResult(S.ERROR, error=f"HTTP {http_status}")


@pytest.mark.parametrize("http_status", [None, 200])
def test_a_transport_error_is_an_error_with_its_text(http_status: int | None) -> None:
    assert classify_doh(NAME, QueryType.A, http_status, "TypeError: Failed to fetch", CLOUDFLARE_A) == QueryResult(S.ERROR, error="TypeError: Failed to fetch")


def test_a_bad_body_is_an_error_with_its_reason_and_no_addresses() -> None:
    result = classify_doh(NAME, QueryType.A, 200, None, body(NAME, A, [record(NAME, A, "256.0.0.1")]))
    assert result.status is S.ERROR
    assert result.addresses == ()
    assert result.error is not None
    assert result.error.startswith(str(P.ADDRESS))


def test_problem_renders_with_its_detail() -> None:
    assert str(DohError(P.LIMIT, "65 addresses")) == f"{P.LIMIT}: 65 addresses"


@pytest.mark.parametrize("sample", [None, [], "text", 0, True, {}, {"Status": "0"}, {"Status": True}, {"Status": -1}, {"Status": 4096}, {"Status": 0.0}])
def test_a_body_without_an_integer_status_is_malformed(sample: JsonValue) -> None:
    assert problem(parse_doh_json(NAME, QueryType.A, sample)) is P.MALFORMED


@pytest.mark.parametrize(
    "question",
    [
        None,
        [],
        {"name": "example.com", "type": 1},
        [{"name": "example.com", "type": 1}, {"name": "example.com", "type": 1}],
        [{"name": "example.org", "type": 1}],
        [{"name": "www.example.com", "type": 1}],
        [{"name": "example.com", "type": 28}],
        [{"name": "example.com", "type": True}],
        [{"name": "example.com"}],
        [{"type": 1}],
        ["example.com"],
    ],
)
def test_the_question_must_be_exactly_the_asked_name_and_type(question: JsonValue) -> None:
    sample: dict[str, JsonValue] = {"Status": 0, "Answer": [record("example.com", A, "192.0.2.1")]}
    if question is not None:
        sample["Question"] = question
    assert problem(parse_doh_json(NAME, QueryType.A, sample)) is P.QUESTION


def test_name_case_and_trailing_dots_do_not_matter() -> None:
    sample = body("Example.COM.", A, [record("EXAMPLE.com.", CNAME, "CDN.Example.NET."), record("cdn.example.net", A, "192.0.2.1")])
    assert parse_doh_json(NAME, QueryType.A, sample) == QueryResult(S.OK, ips("192.0.2.1"), (DomainName("cdn.example.net"),), Seconds(300))


def test_an_error_code_still_checks_the_question() -> None:
    assert problem(parse_doh_json(NAME, QueryType.A, body("example.org", A, status=2))) is P.QUESTION


@pytest.mark.parametrize(
    "answer",
    [
        "records",
        {"name": "example.com"},
        ["record"],
        [{"name": "example.com", "type": 1, "TTL": 300}],
        [record(1, A, "192.0.2.1")],
        [record("example.com", "A", "192.0.2.1")],
        [record("example.com", True, "192.0.2.1")],
        [record("example.com", A, 3221225985)],
        [record("example.com", A, "192.0.2.1", ttl=-1)],
        [record("example.com", A, "192.0.2.1", ttl=2**31)],
        [record("example.com", A, "192.0.2.1", ttl="300")],
        [record("example.com", A, "192.0.2.1", ttl=False)],
        [record("example.com", A, "192.0.2.1", ttl=1.5)],
        [record("example.com", A, "192.0.2.1"), record("other.example", A, "192.0.2.2", ttl=None)],
    ],
)
def test_records_of_the_wrong_shape_are_malformed(answer: JsonValue) -> None:
    sample: JsonValue = {"Status": 0, "Question": [{"name": "example.com", "type": 1}], "Answer": answer}
    assert problem(parse_doh_json(NAME, QueryType.A, sample)) is P.MALFORMED


def test_a_missing_answer_is_empty() -> None:
    assert parse_doh_json(NAME, QueryType.A, {"Status": 0, "Question": [{"name": "example.com", "type": 1}]}) == QueryResult(S.NODATA)


@pytest.mark.parametrize(
    ("qtype", "data"),
    [
        (QueryType.A, "256.0.0.1"),
        (QueryType.A, "192.0.2"),
        (QueryType.A, "01.2.3.4"),
        (QueryType.A, " 192.0.2.1"),
        (QueryType.A, "2001:db8::1"),
        (QueryType.A, ""),
        (QueryType.AAAA, "192.0.2.1"),
        (QueryType.AAAA, "fe80::1%eth0"),
        (QueryType.AAAA, "::ffff:192.0.2.1"),
        (QueryType.AAAA, "2001:db8::g"),
    ],
)
def test_invalid_addresses_reject_the_answer(qtype: QueryType, data: str) -> None:
    assert problem(parse([record(NAME, qtype.rdtype, data)], qtype)) is P.ADDRESS


def test_repeated_addresses_collapse_in_answer_order() -> None:
    answer = [record(NAME, A, "192.0.2.2"), record(NAME, A, "192.0.2.1"), record(NAME, A, "192.0.2.2")]
    assert parse(answer) == QueryResult(S.OK, ips("192.0.2.2", "192.0.2.1"), ttl=Seconds(300))


def test_the_ttl_is_the_smallest_of_the_used_records() -> None:
    answer = [
        record(NAME, CNAME, "cdn.example.net.", ttl=600),
        record("cdn.example.net", A, "192.0.2.1", ttl=60),
        record("cdn.example.net", A, "192.0.2.2", ttl=120),
        record("unrelated.example", A, "192.0.2.3", ttl=1),
    ]
    result = parse(answer)
    assert isinstance(result, QueryResult)
    assert result.ttl == 60


def _addresses(count: int) -> list[JsonValue]:
    return [record(NAME, A, f"10.0.{i // 256}.{i % 256}") for i in range(count)]


def test_up_to_the_address_limit_is_accepted() -> None:
    result = parse(_addresses(MAX_ADDRESSES))
    assert isinstance(result, QueryResult)
    assert len(result.addresses) == MAX_ADDRESSES


def test_more_addresses_than_the_limit_reject_the_answer() -> None:
    assert problem(parse(_addresses(MAX_ADDRESSES + 1))) is P.LIMIT


def test_repeats_do_not_count_against_the_address_limit() -> None:
    assert isinstance(parse(_addresses(MAX_ADDRESSES) * 2), QueryResult)


def test_more_records_than_the_limit_reject_the_answer() -> None:
    noise = [record("other.example", A, "192.0.2.1")] * (MAX_RECORDS - 1)
    assert isinstance(parse([*noise, record(NAME, A, "192.0.2.2")]), QueryResult)
    assert problem(parse([*noise, record(NAME, A, "192.0.2.2"), record(NAME, A, "192.0.2.3")])) is P.LIMIT


def test_records_of_other_names_and_types_are_ignored() -> None:
    answer = [
        record("other.example", A, "192.0.2.9"),
        record("other.example", A, "not an address"),
        record(NAME, AAAA, "2001:db8::1"),
        record(NAME, 46, "A 13 2 300 20261101000000 20261011000000 12345 example.com. c2lnbmF0dXJl"),
        record(NAME, A, "192.0.2.1"),
    ]
    assert parse(answer) == QueryResult(S.OK, ips("192.0.2.1"), ttl=Seconds(300))


def test_only_the_end_of_the_chain_gives_addresses() -> None:
    answer = [record(NAME, A, "192.0.2.9"), record(NAME, CNAME, "cdn.example.net."), record("cdn.example.net", A, "192.0.2.1")]
    assert parse(answer) == QueryResult(S.OK, ips("192.0.2.1"), (DomainName("cdn.example.net"),), Seconds(300))


def test_a_chain_without_addresses_is_nodata_with_the_chain() -> None:
    assert parse([record(NAME, CNAME, "cdn.example.net.")]) == QueryResult(S.NODATA, cnames=(DomainName("cdn.example.net"),), ttl=Seconds(300))


def test_nxdomain_keeps_the_chain() -> None:
    assert parse([record(NAME, CNAME, "gone.example.net.")], status=3) == QueryResult(S.NXDOMAIN, cnames=(DomainName("gone.example.net"),), ttl=Seconds(300))


def test_the_chain_is_followed_regardless_of_record_order() -> None:
    answer = [record("c.example", A, "192.0.2.1"), record("b.example", CNAME, "c.example."), record(NAME, CNAME, "b.example.")]
    assert parse(answer) == QueryResult(S.OK, ips("192.0.2.1"), (DomainName("b.example"), DomainName("c.example")), Seconds(300))


def _chain(length: int) -> list[JsonValue]:
    names = [str(NAME), *(f"c{i}.example" for i in range(length))]
    links = [record(owner, CNAME, target + ".") for owner, target in itertools.pairwise(names)]
    return [*links, record(names[-1], A, "192.0.2.1")]


def test_a_chain_up_to_the_limit_is_followed() -> None:
    result = parse(_chain(MAX_CHAIN))
    assert isinstance(result, QueryResult)
    assert len(result.cnames) == MAX_CHAIN


@pytest.mark.parametrize(
    "answer",
    [
        _chain(MAX_CHAIN + 1),
        [record(NAME, CNAME, "b.example."), record("b.example", CNAME, "example.com.")],
        [record(NAME, CNAME, "example.com.")],
        [record(NAME, CNAME, "b.example."), record(NAME, CNAME, "c.example.")],
        [record(NAME, CNAME, "bad name.example.")],
        [record(NAME, CNAME, "")],
        [record(NAME, CNAME, ".")],
        [record(NAME, CNAME, "a..example.")],
        [record(NAME, CNAME, "x" * 64 + ".example.")],
        [record(NAME, CNAME, ("x" * 63 + ".") * 4 + "example.")],
    ],
)
def test_broken_chains_reject_the_answer(answer: list[JsonValue]) -> None:
    assert problem(parse(answer)) is P.CHAIN


def test_a_repeated_cname_record_is_not_a_fork() -> None:
    answer = [record(NAME, CNAME, "cdn.example.net."), record(NAME, CNAME, "CDN.example.net"), record("cdn.example.net", A, "192.0.2.1")]
    assert parse(answer) == QueryResult(S.OK, ips("192.0.2.1"), (DomainName("cdn.example.net"),), Seconds(300))


def test_cname_targets_may_hold_underscores_and_digits() -> None:
    answer = [record(NAME, CNAME, "_edge-1.cdn123.example."), record("_edge-1.cdn123.example", A, "192.0.2.1")]
    assert parse(answer) == QueryResult(S.OK, ips("192.0.2.1"), (DomainName("_edge-1.cdn123.example"),), Seconds(300))


@given(json_values)
def test_untrusted_json_parses_to_a_result_or_a_problem(payload: JsonValue) -> None:
    result = parse_doh_json(NAME, QueryType.A, payload)
    assert isinstance(result, QueryResult | DohError)
    assert classify_doh(NAME, QueryType.AAAA, 200, None, payload).status is not S.UNREACHABLE
