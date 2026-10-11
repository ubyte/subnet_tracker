from ipaddress import ip_address, ip_network

import pytest

from subnet_tracker.types import (
    PREFIX_V4,
    PREFIX_V6,
    Bytes,
    FieldError,
    ParseError,
    ParseResult,
    Seconds,
    check_prefix,
    format_duration,
    format_size,
    one_line_name,
    one_line_text,
    parse_duration,
    parse_ip_range,
    parse_size,
    zone_error,
)

LINE_BREAKS = ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


@pytest.mark.parametrize("brk", LINE_BREAKS)
def test_line_breaks_are_what_splitlines_splits_on(brk: str) -> None:
    assert len(f"a{brk}b".splitlines()) == 2


@pytest.mark.parametrize("brk", LINE_BREAKS)
@pytest.mark.parametrize("template", ["a{}b", "{}a", "a{}", "  a {} "])
def test_one_line_text_rejects_line_breaks_anywhere(brk: str, template: str) -> None:
    assert one_line_text("comment", template.format(brk)) == FieldError("comment", "line breaks are not allowed")


@pytest.mark.parametrize(("text", "value"), [("", ""), ("   ", ""), ("  static content\t", "static content"), ("a  b", "a  b"), ("\x1f a", "a"), ("тест", "тест")])
def test_one_line_text_trims_edges(text: str, value: str) -> None:
    assert one_line_text("comment", text) == value


@pytest.mark.parametrize(("text", "value"), [(" Video ", "Video"), ("a", "a"), ("x" * 5, "x" * 5), (" " + "x" * 5 + " ", "x" * 5), ("a  b ", "a  b")])
def test_one_line_name_trims_and_keeps_names_within_limit(text: str, value: str) -> None:
    assert one_line_name("name", text, 5) == value


@pytest.mark.parametrize("text", ["", "   ", "x" * 6])
def test_one_line_name_rejects_empty_and_too_long(text: str) -> None:
    assert one_line_name("name", text, 5) == FieldError("name", "must be 1 to 5 characters long")


def test_one_line_name_rejects_line_breaks_first() -> None:
    assert one_line_name("name", "a\nb" * 5, 5) == FieldError("name", "line breaks are not allowed")


@pytest.mark.parametrize(("value", "limits"), [(None, PREFIX_V4), (8, PREFIX_V4), (32, PREFIX_V4), (16, PREFIX_V6), (128, PREFIX_V6)])
def test_check_prefix_accepts_limits_and_inherit(value: int | None, limits: range) -> None:
    assert check_prefix("prefix_v4", value, limits) is None


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("limits", [PREFIX_V4, PREFIX_V6])
def test_check_prefix_rejects_bool(flag: bool, limits: range) -> None:
    assert check_prefix("prefix", flag, limits) == FieldError("prefix", f"prefix must be an integer, got {flag}")


@pytest.mark.parametrize(("value", "limits", "message"), [(7, PREFIX_V4, "8 to 32"), (33, PREFIX_V4, "8 to 32"), (15, PREFIX_V6, "16 to 128"), (129, PREFIX_V6, "16 to 128")])
def test_check_prefix_rejects_out_of_range(value: int, limits: range, message: str) -> None:
    assert check_prefix("prefix", value, limits) == FieldError("prefix", f"prefix /{value} is out of range, expected {message}")


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
