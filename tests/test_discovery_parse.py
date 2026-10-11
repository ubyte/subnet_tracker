from collections.abc import Container
from ipaddress import ip_network

import pytest
from hypothesis import given
from hypothesis import strategies as st

from subnet_tracker.discovery.parse import PastePlan, parse_paste
from subnet_tracker.discovery.www import DomainNames
from subnet_tracker.types import DomainName, ParseResult

NOTHING: frozenset[DomainName] = frozenset()


def parse(text: str, existing: Container[DomainName] = NOTHING) -> ParseResult[PastePlan]:
    return parse_paste(text, existing)


def canonical(text: str) -> list[str]:
    return [d.canonical for d in parse(text).value.new_domains]


def names(first: str, pair: str) -> DomainNames:
    return DomainNames(DomainName(first), DomainName(pair))


@pytest.mark.parametrize(
    ("token", "name"),
    [
        ("example.com", "example.com"),
        ("Example.COM", "example.com"),
        ("example.com.", "example.com"),
        ("example.com。", "example.com"),
        ("example.com:8080", "example.com"),
        ("example.com/path/page.html", "example.com"),
        ("example.com.:443/path?q=1#top", "example.com"),
        ("user@example.com", "example.com"),
        ("user:secret@example.com:8080/x", "example.com"),
        ("//example.com/lib.js", "example.com"),
        ("https://example.com", "example.com"),
        ("HTTPS://WWW.Example.com/Path", "www.example.com"),
        ("http://user:secret@api.example.com:8443/v1?q=1#f", "api.example.com"),
        ("ftp://files.example.com/pub", "files.example.com"),
        ("пример.рф", "xn--e1afmkfd.xn--p1ai"),
        ("ПРИМЕР.РФ", "xn--e1afmkfd.xn--p1ai"),
        ("https://пример.рф/путь", "xn--e1afmkfd.xn--p1ai"),
        ("xn--e1afmkfd.xn--p1ai", "xn--e1afmkfd.xn--p1ai"),
        ("straße.de", "xn--strae-oqa.de"),
        ("example。com", "example.com"),
    ],
)
def test_name_is_normalized(token: str, name: str) -> None:
    result = parse(token)
    assert result.ok
    assert canonical(token) == [name]


@pytest.mark.parametrize(
    ("token", "network"),
    [
        ("1.2.3.4", "1.2.3.4/32"),
        ("1.2.3.0/24", "1.2.3.0/24"),
        ("1.2.3.4/24", "1.2.3.0/24"),
        ("1.2.3.4:80", "1.2.3.4/32"),
        ("http://1.2.3.4:8080/x", "1.2.3.4/32"),
        ("2001:db8::1", "2001:db8::1/128"),
        ("2001:db8::/32", "2001:db8::/32"),
        ("[2001:db8::1]:443", "2001:db8::1/128"),
        ("https://[2001:db8::1]:443/x", "2001:db8::1/128"),
    ],
)
def test_addresses_and_cidr_go_to_networks(token: str, network: str) -> None:
    assert parse(token) == ParseResult(PastePlan(networks=(ip_network(network),)))


def test_networks_keep_input_order_without_repeats() -> None:
    assert parse("10.0.0.0/8 1.2.3.4 1.2.3.4/32 10.1.0.0/8").value.networks == (ip_network("10.0.0.0/8"), ip_network("1.2.3.4/32"))


@pytest.mark.parametrize("token", ["*.example.com", "https://*.example.com/x"])
def test_wildcard_is_added_without_star_with_warning(token: str) -> None:
    assert parse(token) == ParseResult(PastePlan(new_domains=(names("example.com", "www.example.com"),), wildcards=(DomainName("example.com"),)))


def _name_of(length: int) -> str:
    return ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * (length - 196), "com"])


_LONGEST = _name_of(249)


@pytest.mark.parametrize("token", [_LONGEST, "www." + _LONGEST])
def test_name_fits_when_its_pair_fits_253_characters(token: str) -> None:
    assert len(_LONGEST) == 249
    assert canonical(token) == [token]


@pytest.mark.parametrize(
    "token",
    [
        "foo",
        "localhost",
        "https://localhost:8080/",
        "1.2.3",
        "1.2.3.400",
        "exa_mple.com",
        "example..com",
        "example.com..",
        "https://example.com../",
        "-example.com",
        "xn--zz.com",
        "a" * 64 + ".com",
        _name_of(250),
        "www." + _name_of(250),
        "*",
        "*.",
        "*example.com",
        "foo.*.com",
        ".",
        "http://",
        "https://[::1",
        "1.2.3.0/33",
        "2001:db8::/129",
        "fe80::1%eth0",
        "[fe80::1%25eth0]",
    ],
)
def test_invalid_token_is_reported_with_line_and_token(token: str) -> None:
    result = parse(f"example.com\n{token}")
    assert result.value == PastePlan(new_domains=(names("example.com", "www.example.com"),))
    assert [e.line for e in result.errors] == [2]
    assert repr(token) in result.errors[0].message


def test_tokens_are_split_by_whitespace_commas_and_newlines() -> None:
    assert canonical("a.com,b.com\n  c.com\td.com ,, e.com\r\nf.com,\n\ng.com") == ["a.com", "b.com", "c.com", "d.com", "e.com", "f.com", "g.com"]


def test_errors_carry_their_line_numbers() -> None:
    assert [e.line for e in parse("a.com\nfoo\n\nb.com, bar baz\r\nc.com").errors] == [2, 4, 4]


@pytest.mark.parametrize("text", ["", " \n , \t\r\n"])
def test_empty_paste_gives_empty_plan(text: str) -> None:
    assert parse(text) == ParseResult(PastePlan())


@pytest.mark.parametrize(
    ("text", "domains", "duplicates"),
    [
        ("example.com www.example.com", [names("example.com", "www.example.com")], ["www.example.com"]),
        ("www.example.com example.com", [names("www.example.com", "example.com")], ["example.com"]),
        ("Example.com example.com. https://example.com/", [names("example.com", "www.example.com")], ["example.com", "example.com"]),
        ("www.example.com www.www.example.com", [names("www.example.com", "example.com")], ["www.www.example.com"]),
        ("a.com b.com a.com", [names("a.com", "www.a.com"), names("b.com", "www.b.com")], ["a.com"]),
    ],
)
def test_repeats_within_paste_collapse_to_first(text: str, domains: list[DomainNames], duplicates: list[str]) -> None:
    plan = parse(text).value
    assert list(plan.new_domains) == domains
    assert plan.duplicates == tuple(duplicates)


def test_every_name_gets_a_plain_text_pair() -> None:
    assert parse("api.example.com co.uk").value.new_domains == (names("api.example.com", "www.api.example.com"), names("co.uk", "www.co.uk"))
    assert parse("www.api.example.com api.example.com www.co.uk co.uk").value.new_domains == (names("www.api.example.com", "api.example.com"), names("www.co.uk", "co.uk"))


@pytest.mark.parametrize("existing", [{"example.com", "www.example.com"}, {"example.com"}, {"www.example.com"}])
def test_name_or_pair_already_tracked_is_duplicate(existing: set[str]) -> None:
    plan = parse("www.example.com new.com example.com", {DomainName(n) for n in existing}).value
    assert plan.new_domains == (names("new.com", "www.new.com"),)
    assert plan.duplicates == ("www.example.com", "example.com")


_label = st.text(st.sampled_from("abcXYZ019-пРß_*"), min_size=1, max_size=8)
_tokens = st.builds(lambda head, labels, tail: head + ".".join(labels) + tail, st.sampled_from(["", "https://", "//"]), st.lists(_label, min_size=1, max_size=4), st.sampled_from(["", ":80", "/x"]))


@given(st.lists(_tokens, max_size=6))
def test_accepted_names_parse_to_themselves(tokens: list[str]) -> None:
    plan = parse(" ".join(tokens)).value
    accepted = [d.canonical for d in plan.new_domains]
    again = parse(" ".join(accepted))
    assert again.ok
    assert [d.canonical for d in again.value.new_domains] == accepted
