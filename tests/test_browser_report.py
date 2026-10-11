import json
from ipaddress import ip_address

import pytest
from subnet_tracker.resolver.browser_report import BrowserReport, BrowserReportError, ElementRejection, parse_browser_report

from doh_samples import AAAA, CLOUDFLARE_A, CLOUDFLARE_CNAME_A, A, body, record
from subnet_tracker.resolver.model import NameResult
from subnet_tracker.types import DomainName, ResolverId, ResolveStatus

RID = 6
NAME = "example.com"


def element(name: str = NAME, resolver_id: object = RID, qtype: object = "A", http_status: object = 200, body: object = None, error: object = None, **extra: object) -> dict[str, object]:
    item: dict[str, object] = {"name": name, "resolver_id": resolver_id, "type": qtype, "body": body, "error": error}
    if http_status is not _MISSING:
        item["http_status"] = http_status
    item.update(extra)
    return item


class _Missing:
    pass


_MISSING = _Missing()


def parsed(payload: object) -> BrowserReport:
    report = parse_browser_report(payload)  # type: ignore[arg-type]
    assert isinstance(report, BrowserReport)
    return report


def reasons(payload: object) -> list[tuple[int, str]]:
    return [(item.index, item.reason) for item in parsed(payload).rejected]


def pair_of(qtype_a: object = None, qtype_aaaa: object = None) -> list[dict[str, object]]:
    return [element(qtype="A", body=qtype_a if qtype_a is not None else body(NAME, A)), element(qtype="AAAA", body=qtype_aaaa if qtype_aaaa is not None else body(NAME, AAAA))]


def test_a_and_aaaa_of_one_name_fold_into_one_pair() -> None:
    report = parsed([element(qtype="AAAA", body=body(NAME, AAAA)), element(body=CLOUDFLARE_A)])
    assert report.rejected == ()
    pair = report.pairs[0]
    assert (pair.name, pair.resolver_id, pair.indices) == (DomainName(NAME), ResolverId(RID), (1, 0))
    assert pair.result == NameResult(ResolveStatus.OK, (ip_address("93.184.215.14"),))


def test_a_cname_chain_is_the_doh_chain() -> None:
    report = parsed([element(name="www.youtube.com", body=CLOUDFLARE_CNAME_A), element(name="www.youtube.com", qtype="AAAA", body=body("www.youtube.com", AAAA))])
    assert report.pairs[0].result.cnames == (DomainName("youtube-ui.l.google.com"),)
    assert report.pairs[0].result.addresses == (ip_address("142.250.74.14"), ip_address("142.250.74.46"))


def test_different_negatives_are_nodata_and_a_bad_body_is_an_error_without_addresses() -> None:
    nodata = parsed(pair_of(body(NAME, A, status=3), body(NAME, AAAA)))
    assert nodata.pairs[0].result == NameResult(ResolveStatus.NODATA)
    broken = parsed(pair_of(body(NAME, A, [record(NAME, A, "nope")]), body(NAME, AAAA, status=3)))
    assert broken.pairs[0].result.status is ResolveStatus.ERROR
    assert broken.pairs[0].result.addresses == ()
    assert broken.pairs[0].result.error is not None


def test_one_good_family_is_kept_when_the_other_body_is_bad() -> None:
    report = parsed(pair_of(CLOUDFLARE_A, body(NAME, AAAA, [record(NAME, AAAA, "nope")])))
    assert report.pairs[0].result == NameResult(ResolveStatus.OK, (ip_address("93.184.215.14"),), error="address: invalid AAAA address 'nope'")


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ("example.com", "element is not an object"),
        (element(name="Example.com"), "name is not in canonical form"),
        (element(name="example.com."), "name is not in canonical form"),
        (element(qtype="CNAME"), "type is not A or AAAA"),
        (element(qtype="a"), "type is not A or AAAA"),
        (element(resolver_id=0), "resolver_id is not a positive integer"),
        (element(resolver_id=-3), "resolver_id is not a positive integer"),
        (element(resolver_id=True), "resolver_id is not a positive integer"),
        (element(resolver_id="6"), "resolver_id is not a positive integer"),
        (element(http_status=99), "http_status is outside 100..599"),
        (element(http_status=600), "http_status is outside 100..599"),
        (element(http_status="200"), "http_status is outside 100..599"),
        (element(http_status=None, error=None), "neither http_status nor error"),
        (element(http_status=_MISSING, error=None), "neither http_status nor error"),
        (element(http_status=_MISSING, error="   \n"), "neither http_status nor error"),
        (element(error=1), "error is not a string"),
    ],
)
def test_a_malformed_element_is_rejected_on_its_own(item: object, reason: str) -> None:
    report = parsed([item, *pair_of()])
    assert report.rejected[0] == ElementRejection(0, reason)
    assert [pair.indices for pair in report.pairs] == [(1, 2)]


def test_a_missing_partner_and_a_repeated_type_reject_only_that_group() -> None:
    lone = element(name="lone.example", qtype="A")
    repeated = [element(name="dup.example", qtype="A"), element(name="dup.example", qtype="A"), element(name="dup.example", qtype="AAAA")]
    report = parsed([lone, *pair_of(), *repeated])
    assert reasons([lone, *pair_of(), *repeated]) == [(0, "missing the other record type"), (3, "repeated type"), (4, "repeated type"), (5, "repeated type")]
    assert [pair.name for pair in report.pairs] == [DomainName(NAME)]


def test_a_transport_error_pairs_with_its_partner_and_is_not_unreachable() -> None:
    raw = "boom\n" + ("x" * 250)
    report = parsed([element(qtype="A", http_status=None, error=raw, body=None), element(qtype="AAAA", http_status=404, body=CLOUDFLARE_A)])
    result = report.pairs[0].result
    assert result.status is ResolveStatus.ERROR
    assert result.addresses == ()
    assert result.error is not None
    assert "\n" not in result.error
    assert len(result.error) <= 200
    assert result.error.startswith("boom ")
    assert "HTTP 404" in result.error


def test_fetch_failure_without_http_status_is_an_error() -> None:
    report = parsed([element(qtype="A", http_status=_MISSING, error="Failed to fetch"), element(qtype="AAAA", http_status=_MISSING, error="Failed to fetch")])
    assert report.rejected == ()
    assert report.pairs[0].result.status is ResolveStatus.ERROR
    assert report.pairs[0].result.error == "A: Failed to fetch; AAAA: Failed to fetch"


def test_more_than_500_elements_or_a_non_array_rejects_the_whole_report() -> None:
    assert parse_browser_report({"name": NAME}) == BrowserReportError("not a JSON array")
    assert parse_browser_report(json.loads("null")) == BrowserReportError("not a JSON array")
    assert parse_browser_report([element()] * 501) == BrowserReportError("more than 500 elements")
    report = parsed([{}] * 500)
    assert report.pairs == ()
    assert len(report.rejected) == 500


def test_pairs_keep_the_order_of_their_first_element() -> None:
    first = [element(name="a.example", qtype="A"), element(name="a.example", qtype="AAAA")]
    second = [element(name="b.example", qtype="AAAA"), element(name="b.example", qtype="A")]
    assert [pair.name for pair in parsed([*second, *first]).pairs] == [DomainName("b.example"), DomainName("a.example")]
