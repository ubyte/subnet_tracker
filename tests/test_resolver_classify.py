import errno
from collections.abc import Sequence
from ipaddress import ip_address

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import dns.rrset
import pytest

from subnet_tracker.resolver.classify import Classified, classify_answer, classify_pair
from subnet_tracker.types import IPAddress, ResolveStatus

S = ResolveStatus
Record = tuple[str, str, int, str]
_MAPPED = "IPv4-mapped addresses are not routable, use the IPv4 form"


def answer_of(name: str, qtype: str, message: dns.message.Message | None, failure: BaseException | None) -> Classified:
    result = classify_answer(name, qtype, message, failure)
    assert result.status is not S.NOTFOUND
    assert (result.status is S.OK) == bool(result.addresses)
    assert "ttl" not in Classified.__dataclass_fields__
    if result.error is not None:
        assert len(result.error) <= 200
        assert "".join(result.error.splitlines()) == result.error
    return result


def response(
    name: str = "example.com",
    qtype: str = "A",
    rcode: str | int = "NOERROR",
    answer: Sequence[Record] = (),
    authority: Sequence[Record] = (),
    additional: Sequence[Record] = (),
) -> dns.message.Message:
    query = dns.message.make_query(name, qtype)
    query.flags |= dns.flags.QR
    query.set_rcode(dns.rcode.Rcode.make(rcode))
    for section, records in ((query.answer, answer), (query.authority, authority), (query.additional, additional)):
        for owner, rdtype, ttl, rdata in records:
            section.append(dns.rrset.from_text(owner if owner.endswith(".") else f"{owner}.", ttl, "IN", rdtype, rdata))
    return query


def addresses(n: int, owner: str = "example.com.") -> dns.rrset.RRset:
    texts = [f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}" for i in range(n)]
    return dns.rrset.from_text_list(owner, 30, "IN", "A", texts)


def chain(links: int, address: str | None = "1.2.3.4") -> dns.message.Message:
    records: list[Record] = [(f"c{i}.example.com", "CNAME", 30, f"c{i + 1}.example.com.") for i in range(links)]
    if address is not None:
        records.append((f"c{links}.example.com", "A", 30, address))
    return response("c0.example.com", answer=records)


def caused(inner: BaseException) -> BaseException:
    outer = RuntimeError("query failed")
    outer.__cause__ = inner
    return outer


def dns_timeout(seconds: float | None = None) -> BaseException:
    if seconds is None:
        return dns.exception.Timeout()  # type: ignore[no-untyped-call]
    return dns.exception.Timeout(timeout=seconds)  # type: ignore[no-untyped-call]


def lifetime_timeout() -> BaseException:
    return dns.resolver.LifetimeTimeout()  # type: ignore[no-untyped-call]


def contextual(inner: BaseException) -> BaseException:
    try:
        raise inner
    except BaseException:
        try:
            raise RuntimeError("query failed")
        except RuntimeError as outer:
            return outer


def ips(*texts: str) -> tuple[IPAddress, ...]:
    return tuple(ip_address(text) for text in texts)


def test_address_answer_is_ok() -> None:
    query = response(answer=(("example.com", "A", 30, "192.0.2.1"), ("example.com", "A", 30, "192.0.2.1"), ("example.com", "A", 40, "192.0.2.2")))
    assert answer_of("example.com", "A", query, None) == Classified(S.OK, ips("192.0.2.1", "192.0.2.2"))


def test_name_and_type_match_ignores_case_and_the_trailing_dot() -> None:
    query = response(answer=(("Example.COM", "A", 30, "192.0.2.1"),))
    assert answer_of("Example.COM.", "a", query, None) == Classified(S.OK, ips("192.0.2.1"))


def test_relative_owner_matches_the_absolute_question() -> None:
    query = response()
    query.answer.append(dns.rrset.from_text("example.com", 30, "IN", "A", "192.0.2.1"))
    assert not dns.rrset.from_text("example.com", 30, "IN", "A", "192.0.2.1").name.is_absolute()
    assert answer_of("example.com", "A", query, None) == Classified(S.OK, ips("192.0.2.1"))


def test_aaaa_normalizes_and_keeps_order() -> None:
    query = response("example.com", "AAAA", answer=(("example.com", "AAAA", 30, "2001:0db8:0000::0002"), ("example.com", "AAAA", 30, "2001:db8::1"), ("example.com", "AAAA", 30, "2001:db8::2")))
    assert answer_of("example.com", "AAAA", query, None) == Classified(S.OK, ips("2001:db8::2", "2001:db8::1"))


def test_ttl_zero_and_the_maximum_are_accepted_and_not_stored() -> None:
    assert answer_of("example.com", "A", response(answer=(("example.com", "A", 0, "0.0.0.0"),)), None) == Classified(S.OK, ips("0.0.0.0"))
    assert answer_of("example.com", "AAAA", response("example.com", "AAAA", answer=(("example.com", "AAAA", 2147483647, "::"),)), None) == Classified(S.OK, ips("::"))


def test_empty_noerror_is_nodata() -> None:
    assert answer_of("example.com", "A", response(), None) == Classified(S.NODATA)


def test_other_type_is_nodata() -> None:
    query = response(answer=(("example.com", "AAAA", 30, "2001:db8::1"),))
    assert answer_of("example.com", "A", query, None) == Classified(S.NODATA)


def test_authority_and_additional_are_ignored() -> None:
    records = (("example.com", "A", 30, "192.0.2.1"),)
    assert answer_of("example.com", "A", response(authority=records), None) == Classified(S.NODATA)
    assert answer_of("example.com", "A", response(additional=records), None) == Classified(S.NODATA)


def test_glue_in_additional_is_not_an_address() -> None:
    query = response(answer=(("example.com", "CNAME", 30, "cdn.example.net."),), additional=(("cdn.example.net", "A", 30, "192.0.2.1"),))
    assert answer_of("example.com", "A", query, None) == Classified(S.NODATA, cnames=("cdn.example.net",))


def test_other_names_and_their_ttls_are_ignored() -> None:
    query = response(answer=(("other.example.net", "A", 2147483648, "9.9.9.9"), ("example.com", "AAAA", -1, "2001:db8::1"), ("example.com", "A", 10, "192.0.2.1")))
    assert answer_of("example.com", "A", query, None) == Classified(S.OK, ips("192.0.2.1"))


def test_empty_failure_text_is_error() -> None:
    assert answer_of("example.com", "A", None, RuntimeError()) == Classified(S.ERROR, error="error")


def test_malformed_address_rdata_is_an_error() -> None:
    query = response()
    rrset = dns.rrset.RRset(dns.name.from_text("example.com."), dns.rdataclass.IN, dns.rdatatype.A)
    rrset.update_ttl(30)
    rrset.add(dns.rdata.GenericRdata(dns.rdataclass.IN, dns.rdatatype.A, b"\x01\x02"))
    query.answer.append(rrset)
    assert answer_of("example.com", "A", query, None) == Classified(S.ERROR, error="invalid address")


def test_other_class_is_ignored() -> None:
    query = response(answer=(("example.com", "A", 30, "192.0.2.1"),))
    foreign = dns.rrset.RRset(dns.name.from_text("example.com."), dns.rdataclass.CH, dns.rdatatype.A)
    foreign.update_ttl(1)
    foreign.add(dns.rdata.GenericRdata(dns.rdataclass.CH, dns.rdatatype.A, b"\x09\x09\x09\x09"))
    query.answer.append(foreign)
    assert answer_of("example.com", "A", query, None) == Classified(S.OK, ips("192.0.2.1"))
    only = response()
    only.answer.append(foreign)
    assert answer_of("example.com", "A", only, None) == Classified(S.NODATA)


def test_cname_chain_ends_at_the_address() -> None:
    query = response(
        answer=(
            ("example.com", "CNAME", 50, "Www.Example.COM."),
            ("www.example.com", "CNAME", 40, "cdn.example.net."),
            ("cdn.example.net", "A", 30, "192.0.2.1"),
            ("example.com", "A", 5, "203.0.113.1"),
        )
    )
    assert answer_of("example.com", "A", query, None) == Classified(S.OK, ips("192.0.2.1"), ("www.example.com", "cdn.example.net"))


def test_cname_without_an_address_is_nodata() -> None:
    query = response(answer=(("example.com", "CNAME", 30, "cdn.example.net."), ("example.com", "A", 30, "192.0.2.1")))
    assert answer_of("example.com", "A", query, None) == Classified(S.NODATA, cnames=("cdn.example.net",))


def test_repeated_cname_target_is_not_a_fork() -> None:
    query = response(answer=(("example.com", "CNAME", 30, "cdn.example.net."), ("example.com", "CNAME", 40, "CDN.Example.NET."), ("cdn.example.net", "A", 30, "192.0.2.1")))
    assert answer_of("example.com", "A", query, None) == Classified(S.OK, ips("192.0.2.1"), ("cdn.example.net",))


def test_cname_fork_is_an_error_without_addresses() -> None:
    query = response(answer=(("example.com", "CNAME", 30, "a.example.net."), ("example.com", "CNAME", 30, "b.example.net."), ("a.example.net", "A", 30, "192.0.2.1")))
    assert answer_of("example.com", "A", query, None) == Classified(S.ERROR, error="cname fork")


@pytest.mark.parametrize("target", ["example.com.", "Example.COM."])
def test_cname_cycle_is_an_error_without_addresses(target: str) -> None:
    query = response(answer=(("example.com", "CNAME", 30, "loop.example.net."), ("loop.example.net", "CNAME", 30, target), ("example.com", "A", 30, "192.0.2.1")))
    assert answer_of("example.com", "A", query, None) == Classified(S.ERROR, error="cname cycle")


def test_cname_chain_of_16_is_ok_and_17_is_an_error() -> None:
    followed = tuple(f"c{i}.example.com" for i in range(1, 17))
    assert answer_of("c0.example.com", "A", chain(16), None) == Classified(S.OK, ips("1.2.3.4"), followed)
    assert answer_of("c0.example.com", "A", chain(17), None) == Classified(S.ERROR, error="cname chain is longer than 16")


def test_nxdomain_keeps_the_chain_and_drops_addresses() -> None:
    query = response(rcode="NXDOMAIN", answer=(("example.com", "CNAME", 30, "cdn.example.net."), ("cdn.example.net", "A", 2147483648, "192.0.2.1")))
    assert answer_of("example.com", "A", query, None) == Classified(S.NXDOMAIN, cnames=("cdn.example.net",))


def test_nxdomain_with_many_addresses_stays_nxdomain() -> None:
    query = response(rcode="NXDOMAIN")
    query.answer.append(addresses(65))
    assert answer_of("example.com", "A", query, None) == Classified(S.NXDOMAIN)


def test_nxdomain_rejects_a_broken_chain() -> None:
    query = response(rcode="NXDOMAIN", answer=(("example.com", "CNAME", 2147483648, "cdn.example.net."),))
    assert answer_of("example.com", "A", query, None) == Classified(S.ERROR, error="ttl out of range")


@pytest.mark.parametrize(("ttl", "where"), [(-1, "address"), (2147483648, "address"), (-1, "cname"), (2147483648, "cname")])
def test_ttl_outside_range_is_an_error_without_addresses(ttl: int, where: str) -> None:
    if where == "address":
        query = response(answer=(("example.com", "CNAME", 30, "cdn.example.net."), ("cdn.example.net", "A", ttl, "192.0.2.1")))
        assert answer_of("example.com", "A", query, None) == Classified(S.ERROR, cnames=("cdn.example.net",), error="ttl out of range")
    else:
        query = response(answer=(("example.com", "CNAME", ttl, "cdn.example.net."), ("cdn.example.net", "A", 30, "192.0.2.1")))
        assert answer_of("example.com", "A", query, None) == Classified(S.ERROR, error="ttl out of range")


def test_ipv4_mapped_aaaa_is_an_error_without_addresses() -> None:
    query = response(
        "example.com", "AAAA", answer=(("example.com", "CNAME", 30, "cdn.example.net."), ("cdn.example.net", "AAAA", 30, "2001:db8::1"), ("cdn.example.net", "AAAA", 30, "::ffff:192.0.2.1"))
    )
    assert answer_of("example.com", "AAAA", query, None) == Classified(S.ERROR, cnames=("cdn.example.net",), error=_MAPPED)


def test_ipv4_mapped_aaaa_does_not_affect_an_a_query() -> None:
    query = response(answer=(("example.com", "A", 30, "192.0.2.1"), ("example.com", "AAAA", 30, "::ffff:192.0.2.1")))
    assert answer_of("example.com", "A", query, None) == Classified(S.OK, ips("192.0.2.1"))


def test_64_addresses_are_ok_and_65_is_an_error() -> None:
    query = response()
    query.answer.append(addresses(64))
    result = answer_of("example.com", "A", query, None)
    assert result.status is S.OK
    assert len(result.addresses) == 64
    assert result.addresses[0] == ip_address("10.0.0.0")
    assert result.addresses[-1] == ip_address("10.0.0.63")
    over = response()
    over.answer.append(addresses(65))
    assert answer_of("example.com", "A", over, None) == Classified(S.ERROR, error="more than 64 addresses")


def test_duplicate_addresses_do_not_count_toward_64() -> None:
    query = response()
    query.answer.append(addresses(1))
    query.answer.append(addresses(1))
    assert answer_of("example.com", "A", query, None) == Classified(S.OK, ips("10.0.0.0"))


def test_other_names_do_not_count_toward_64_addresses() -> None:
    query = response(answer=(("example.com", "A", 30, "192.0.2.1"),))
    query.answer.append(addresses(100, "other.example.net."))
    assert answer_of("example.com", "A", query, None) == Classified(S.OK, ips("192.0.2.1"))


def test_256_answer_records_are_kept_and_257_is_an_error() -> None:
    kept = response(answer=(("example.com", "A", 30, "192.0.2.1"),))
    kept.answer.append(addresses(255, "other.example.net."))
    assert answer_of("example.com", "A", kept, None) == Classified(S.OK, ips("192.0.2.1"))
    ignored = response()
    ignored.answer.append(addresses(256, "other.example.net."))
    assert answer_of("example.com", "A", ignored, None) == Classified(S.NODATA)
    over = response(answer=(("example.com", "A", 30, "192.0.2.1"),))
    over.answer.append(addresses(256, "other.example.net."))
    assert answer_of("example.com", "A", over, None) == Classified(S.ERROR, error="more than 256 answer records")


@pytest.mark.parametrize(
    ("rcode", "reason"), [("SERVFAIL", "SERVFAIL"), ("REFUSED", "REFUSED"), ("FORMERR", "FORMERR"), ("NOTIMP", "NOTIMP"), ("NXRRSET", "NXRRSET"), ("BADCOOKIE", "BADCOOKIE"), (15, "15")]
)
def test_other_rcodes_are_errors_and_the_answer_is_not_parsed(rcode: str | int, reason: str) -> None:
    query = response(rcode=rcode, answer=(("example.com", "CNAME", 30, "example.com."), ("example.com", "A", 30, "192.0.2.1")))
    assert answer_of("example.com", "A", query, None) == Classified(S.ERROR, error=reason)


@pytest.mark.parametrize(
    ("name", "qtype", "asked", "asked_type"),
    [("other.example.net", "A", "example.com", "A"), ("example.com", "AAAA", "example.com", "A"), ("example.com", "A", "example.com", "A")],
)
def test_question_must_be_the_requested_name_and_type(name: str, qtype: str, asked: str, asked_type: str) -> None:
    query = response(name, qtype, rcode="SERVFAIL", answer=(("example.com", "A", 30, "192.0.2.1"),))
    if (name, qtype) == (asked, asked_type):
        assert answer_of(asked, asked_type, query, None) == Classified(S.ERROR, error="SERVFAIL")
    else:
        assert answer_of(asked, asked_type, query, None) == Classified(S.ERROR, error="question does not match")


def test_zero_or_two_questions_do_not_match() -> None:
    empty = response(answer=(("example.com", "A", 30, "192.0.2.1"),))
    empty.question.clear()
    extra = response(answer=(("example.com", "A", 30, "192.0.2.1"),))
    extra.question.append(dns.message.make_query("example.com", "AAAA").question[0])
    assert answer_of("example.com", "A", empty, None) == Classified(S.ERROR, error="question does not match")
    assert answer_of("example.com", "A", extra, None) == Classified(S.ERROR, error="question does not match")


@pytest.mark.parametrize("failure", [dns_timeout(), dns_timeout(1.5), TimeoutError("timed out"), lifetime_timeout(), OSError(errno.ETIMEDOUT, "Connection timed out")])
def test_timeout_is_an_error_with_that_text(failure: BaseException) -> None:
    assert answer_of("example.com", "A", response(answer=(("example.com", "A", 30, "192.0.2.1"),)), failure) == Classified(S.ERROR, error="timeout")


@pytest.mark.parametrize(
    ("err", "reason"),
    [
        (errno.ENETUNREACH, "ENETUNREACH"),
        (errno.EHOSTUNREACH, "EHOSTUNREACH"),
        (errno.ENONET, "ENONET"),
        (errno.ENETDOWN, "ENETDOWN"),
        (errno.EHOSTDOWN, "EHOSTDOWN"),
        (errno.EAFNOSUPPORT, "EAFNOSUPPORT"),
        (errno.EADDRNOTAVAIL, "EADDRNOTAVAIL"),
    ],
)
def test_unreachable_errno_is_unreachable(err: int, reason: str) -> None:
    failure = OSError(err, "network")
    assert answer_of("example.com", "A", None, failure) == Classified(S.UNREACHABLE, error=reason)


@pytest.mark.parametrize("err", [errno.ECONNREFUSED, errno.ECONNRESET, errno.EPIPE])
def test_other_socket_errors_are_errors(err: int) -> None:
    failure = OSError(err, "failed")
    assert answer_of("example.com", "A", None, failure) == Classified(S.ERROR, error=" ".join(str(failure).split()))


def test_reason_is_one_line_of_at_most_200_characters() -> None:
    failure = RuntimeError("a" * 150 + "\n\n" + "b" * 150)
    assert answer_of("example.com", "A", None, failure).error == "a" * 150 + " " + "b" * 49


def test_nested_cause_and_context_are_read() -> None:
    assert answer_of("example.com", "A", None, caused(dns_timeout())) == Classified(S.ERROR, error="timeout")
    assert answer_of("example.com", "A", None, contextual(OSError(errno.EADDRNOTAVAIL, "addr"))) == Classified(S.UNREACHABLE, error="EADDRNOTAVAIL")
    refused = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    assert answer_of("example.com", "A", None, caused(refused)) == Classified(S.ERROR, error=" ".join(str(refused).split()))


def test_nearest_failure_wins() -> None:
    timeout = dns_timeout()
    timeout.__cause__ = OSError(errno.ENETUNREACH, "net")
    assert answer_of("example.com", "A", None, timeout) == Classified(S.ERROR, error="timeout")
    offline = OSError(errno.ENETUNREACH, "net")
    offline.__cause__ = dns_timeout()
    assert answer_of("example.com", "A", None, offline) == Classified(S.UNREACHABLE, error="ENETUNREACH")


def test_query_type_must_be_address_type() -> None:
    with pytest.raises(ValueError, match="unsupported query type"):
        classify_answer("example.com", "MX", response(), None)
    with pytest.raises(ValueError, match="unsupported query type"):
        classify_answer("example.com", "nope", response(), None)


def test_message_or_failure_is_required() -> None:
    with pytest.raises(ValueError, match="a message or a transport failure is required"):
        classify_answer("example.com", "A", None, None)


@pytest.mark.parametrize(
    ("a", "aaaa", "expected"),
    [
        (Classified(S.NODATA), Classified(S.NODATA), Classified(S.NODATA)),
        (Classified(S.NXDOMAIN), Classified(S.NXDOMAIN), Classified(S.NXDOMAIN)),
        (Classified(S.NOTFOUND), Classified(S.NOTFOUND), Classified(S.NOTFOUND)),
        (Classified(S.NXDOMAIN), Classified(S.NODATA), Classified(S.NODATA)),
        (Classified(S.NODATA), Classified(S.NXDOMAIN), Classified(S.NODATA)),
        (Classified(S.NOTFOUND), Classified(S.NXDOMAIN), Classified(S.NODATA)),
        (Classified(S.NXDOMAIN), Classified(S.NOTFOUND), Classified(S.NODATA)),
        (Classified(S.UNREACHABLE, error="ENETUNREACH"), Classified(S.UNREACHABLE, error="EHOSTDOWN"), Classified(S.UNREACHABLE, error="ENETUNREACH")),
        (Classified(S.UNREACHABLE, error="ENETUNREACH"), Classified(S.ERROR, error="timeout"), Classified(S.ERROR, error="ENETUNREACH")),
        (Classified(S.ERROR, error="timeout"), Classified(S.UNREACHABLE, error="ENETUNREACH"), Classified(S.ERROR, error="timeout")),
        (Classified(S.ERROR, error="timeout"), Classified(S.ERROR, error="SERVFAIL"), Classified(S.ERROR, error="timeout")),
        (Classified(S.ERROR, error="timeout"), Classified(S.NODATA), Classified(S.ERROR, error="timeout")),
        (Classified(S.NODATA), Classified(S.UNREACHABLE, error="ENETDOWN"), Classified(S.ERROR, error="ENETDOWN")),
        (Classified(S.NXDOMAIN), Classified(S.ERROR, error="SERVFAIL"), Classified(S.ERROR, error="SERVFAIL")),
        (Classified(S.NOTFOUND), Classified(S.ERROR, error="timeout"), Classified(S.ERROR, error="timeout")),
        (Classified(S.OK, ips("192.0.2.1")), Classified(S.NODATA), Classified(S.OK, ips("192.0.2.1"))),
        (Classified(S.OK, ips("192.0.2.1")), Classified(S.NXDOMAIN), Classified(S.OK, ips("192.0.2.1"))),
        (Classified(S.OK, ips("192.0.2.1")), Classified(S.ERROR, error="timeout"), Classified(S.OK, ips("192.0.2.1"), error="timeout")),
        (Classified(S.UNREACHABLE, error="ENETUNREACH"), Classified(S.OK, ips("2001:db8::1")), Classified(S.OK, ips("2001:db8::1"), error="ENETUNREACH")),
        (Classified(S.OK, ips("192.0.2.1")), Classified(S.OK, ips("2001:db8::1")), Classified(S.OK, ips("192.0.2.1", "2001:db8::1"))),
    ],
)
def test_pair_status(a: Classified, aaaa: Classified, expected: Classified) -> None:
    assert classify_pair(a, aaaa) == expected


def test_pair_cnames_follow_a_then_aaaa_without_repeats() -> None:
    a = Classified(S.NXDOMAIN, cnames=("a.example.net", "b.example.net"))
    aaaa = Classified(S.NODATA, cnames=("b.example.net", "c.example.net"))
    assert classify_pair(a, aaaa) == Classified(S.NODATA, cnames=("a.example.net", "b.example.net", "c.example.net"))


def test_pair_keeps_a_chain_beside_a_failure() -> None:
    a = Classified(S.NXDOMAIN, cnames=("cdn.example.net",))
    aaaa = Classified(S.ERROR, error="timeout")
    assert classify_pair(a, aaaa) == Classified(S.ERROR, cnames=("cdn.example.net",), error="timeout")


def test_pair_from_two_messages() -> None:
    a = answer_of("example.com", "A", response(answer=(("example.com", "CNAME", 30, "cdn.example.net."), ("cdn.example.net", "A", 30, "192.0.2.1"))), None)
    aaaa = answer_of("example.com", "AAAA", response("example.com", "AAAA", answer=(("example.com", "CNAME", 30, "cdn.example.net."), ("cdn.example.net", "AAAA", 30, "2001:db8::1"))), None)
    assert classify_pair(a, aaaa) == Classified(S.OK, ips("192.0.2.1", "2001:db8::1"), ("cdn.example.net",))
    offline = answer_of("example.com", "AAAA", None, OSError(errno.ENETUNREACH, "net"))
    assert classify_pair(a, offline) == Classified(S.OK, ips("192.0.2.1"), ("cdn.example.net",), "ENETUNREACH")
    nodata = answer_of("example.com", "AAAA", response("example.com", "AAAA"), None)
    nxdomain = answer_of("example.com", "A", response(rcode="NXDOMAIN"), None)
    assert classify_pair(nxdomain, nodata) == Classified(S.NODATA)
