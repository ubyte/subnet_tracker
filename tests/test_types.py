from ipaddress import ip_address

import pytest

from subnet_tracker.types import ParseError, ParseResult, parse_duration, parse_ip_range


@pytest.mark.parametrize(("text", "seconds"), [("0s", 0), ("3s", 3), ("60m", 3600), ("24h", 86400), ("365d", 365 * 86400)])
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "5", "m", "-1s", "1.5h", "1w", "1h30m", " 1h"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(text)


@pytest.mark.parametrize(
    ("text", "first", "last"),
    [
        ("10.8.0.0/24", "10.8.0.0", "10.8.0.255"),
        ("127.0.0.0/8", "127.0.0.0", "127.255.255.255"),
        ("10.8.0.5", "10.8.0.5", "10.8.0.5"),
        ("::1", "::1", "::1"),
        ("10.8.0.2-10.8.0.9", "10.8.0.2", "10.8.0.9"),
        ("fd00::2-fd00::9", "fd00::2", "fd00::9"),
        ("2001:db8::/126", "2001:db8::", "2001:db8::3"),
    ],
)
def test_parse_ip_range(text: str, first: str, last: str) -> None:
    r = parse_ip_range(text)
    assert (r.first, r.last) == (ip_address(first), ip_address(last))


@pytest.mark.parametrize("text", ["", "10.8.0.9-10.8.0.2", "10.8.0.1-::1", "10.8.0.0/33", "10.8.0.1/24", "host", "10.8.0.1-", "1-2-3"])
def test_parse_ip_range_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="range"):
        parse_ip_range(text)


def test_ip_range_contains_bounds() -> None:
    r = parse_ip_range("10.8.0.2-10.8.0.9")
    assert ip_address("10.8.0.2") in r
    assert ip_address("10.8.0.9") in r
    assert ip_address("10.8.0.1") not in r
    assert ip_address("10.8.0.10") not in r
    assert ip_address("::ffff:10.8.0.5") not in r


def test_ipv6_range_contains() -> None:
    r = parse_ip_range("fd00::2-fd00::9")
    assert ip_address("fd00::5") in r
    assert ip_address("fd00::a") not in r
    assert ip_address("10.8.0.5") not in r


def test_parse_result_ok() -> None:
    assert ParseResult(value=1).ok
    assert not ParseResult(value=1, errors=(ParseError(line=3, message="bad"),)).ok
