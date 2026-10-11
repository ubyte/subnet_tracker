import pytest

from domains_world import dn
from subnet_tracker.discovery.parse import parse_names, unicode_name
from subnet_tracker.types import DomainName, ParseResult


def test_names_are_normalized_in_input_order_with_repeats() -> None:
    assert parse_names("Example.COM. www.a.com,a.com\nBücher.de\n\nexample.com") == ParseResult(dn("example.com", "www.a.com", "a.com", "xn--bcher-kva.de", "example.com"))


@pytest.mark.parametrize(("token", "reason"), [("https://a.com/", "Codepoint"), ("1.2.3.4", "numeric top-level label"), ("*.a.com", "wildcards are not supported"), ("foo", "a single label")])
def test_names_list_rejects_what_is_not_a_name(token: str, reason: str) -> None:
    result = parse_names(f"a.com\nb.com {token}")
    assert result.value == dn("a.com", "b.com")
    ((line, message),) = [(e.line, e.message) for e in result.errors]
    assert (line, message.startswith(f"{token!r}: "), reason in message) == (2, True, True)


@pytest.mark.parametrize(("name", "shown"), [("xn--e1afmkfd.xn--p1ai", "пример.рф"), ("www.xn--bcher-kva.de", "www.bücher.de"), ("a.com", "a.com"), ("xn--zz.com", "xn--zz.com")])
def test_unicode_name_decodes_punycode_and_keeps_the_rest(name: str, shown: str) -> None:
    assert unicode_name(DomainName(name)) == shown
