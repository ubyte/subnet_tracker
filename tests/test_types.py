from ipaddress import ip_address, ip_network

import pytest

from subnet_tracker.types import Bytes, ParseError, ParseResult, Seconds, format_duration, format_size, parse_duration, parse_ip_range, parse_size, zone_error


@pytest.mark.parametrize("text", ["10.0.0.0/8", "192.0.2.1", "2001:db8::/32", "fe80::1", "::/0"])
def test_zone_error_accepts_networks_without_zone(text: str) -> None:
    assert zone_error(ip_network(text)) is None


@pytest.mark.parametrize("text", ["fe80::1%eth0", "fe80::%eth0/64", "fe80::1%1"])
def test_zone_error_rejects_ipv6_zone(text: str) -> None:
    assert zone_error(ip_network(text)) == "zone index is not routable"


@pytest.mark.parametrize(("text", "seconds"), [("0s", 0), ("3s", 3), ("60m", 3600), ("24h", 86400), ("365d", 365 * 86400)])
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "5", "m", "-1s", "1.5h", "1w", "1h30m", " 1h"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(text)


@pytest.mark.parametrize(("seconds", "text"), [(0, "0s"), (3, "3s"), (90, "90s"), (60, "1m"), (5400, "90m"), (3600, "1h"), (86400, "1d"), (90000, "25h"), (365 * 86400, "365d")])
def test_format_duration(seconds: int, text: str) -> None:
    assert format_duration(Seconds(seconds)) == text
    assert parse_duration(text) == seconds


@pytest.mark.parametrize(("text", "size"), [("0B", 0), ("1B", 1), ("512KB", 512 * 1024), ("5MB", 5 * 1024**2), ("2GB", 2 * 1024**3)])
def test_parse_size(text: str, size: int) -> None:
    assert parse_size(text) == size


@pytest.mark.parametrize("text", ["", "5", "MB", "-1KB", "1.5MB", "5 MB", "5mb", "5TB", "5M", " 5MB"])
def test_parse_size_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="size"):
        parse_size(text)


@pytest.mark.parametrize(("size", "text"), [(0, "0B"), (1000, "1000B"), (1024, "1KB"), (1536, "1536B"), (1024**2, "1MB"), (1536 * 1024, "1536KB"), (5 * 1024**2, "5MB"), (1024**3, "1GB")])
def test_format_size(size: int, text: str) -> None:
    assert format_size(Bytes(size)) == text
    assert parse_size(text) == size


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
