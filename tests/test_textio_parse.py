from ipaddress import ip_network

import pytest

from subnet_tracker.textio.model import DomainLine, DuplicateLine, GroupSection, ImportFile, RouteLine, SettingLine
from subnet_tracker.textio.parse import parse_import
from subnet_tracker.types import DomainName, ParseResult

DESIGN_EXAMPLE = """\
# subnet-tracker export v1

[settings]
resolve.interval = 60m
routes.default_prefix_v4 = 23

[group "Video"]
description = Video hosting
prefix_v4 = 24

youtube.com                    #video #google
googlevideo.com    /16         #video        @Google
ytimg.com                      #video        ; static content

[ungrouped]
example.org        /22 v6/40   !disabled
www.example.net                             ; canonical name with www, pair example.net

[routes]
149.154.160.0/20   @Messengers   ; Telegram ranges
192.168.10.0/24                  ; LAN behind the server
"""


def domain(line: int, name: str, **attrs: object) -> DomainLine:
    return DomainLine(line, DomainName(name), **attrs)  # type: ignore[arg-type]


def domains(text: str) -> tuple[DomainLine, ...]:
    result = parse_import(text)
    assert result.errors == ()
    return result.value.domains


def error_lines(text: str) -> list[int]:
    return [e.line for e in parse_import(text).errors]


def test_design_example() -> None:
    assert parse_import(DESIGN_EXAMPLE) == ParseResult(
        ImportFile(
            settings=(SettingLine(4, "resolve.interval", "60m"), SettingLine(5, "routes.default_prefix_v4", "23")),
            groups=(GroupSection(7, "Video", description="Video hosting", prefix_v4=24),),
            domains=(
                domain(11, "youtube.com", groups=("Video",), tags=("video", "google")),
                domain(12, "googlevideo.com", groups=("Video", "Google"), tags=("video",), prefix_v4=16),
                domain(13, "ytimg.com", groups=("Video",), tags=("video",), comment="static content"),
                domain(16, "example.org", prefix_v4=22, prefix_v6=40, enabled=False),
                domain(17, "www.example.net", comment="canonical name with www, pair example.net"),
            ),
            routes=(
                RouteLine(20, ip_network("149.154.160.0/20"), groups=("Messengers",), comment="Telegram ranges"),
                RouteLine(21, ip_network("192.168.10.0/24"), comment="LAN behind the server"),
            ),
        )
    )


@pytest.mark.parametrize("text", ["", "\n  \n", "# just a comment\n\n# subnet-tracker export v1\n"])
def test_empty_file(text: str) -> None:
    assert parse_import(text) == ParseResult(ImportFile())


def test_plain_domain_list_is_ungrouped() -> None:
    assert domains("example.com\nwww.example.net\r\n  api.example.org  \n") == (domain(1, "example.com"), domain(2, "www.example.net"), domain(3, "api.example.org"))


def test_canonical_name_with_www_is_kept() -> None:
    assert domains("www.example.com") == (domain(1, "www.example.com"),)


@pytest.mark.parametrize(("text", "name"), [("Example.COM.", "example.com"), ("пример.рф", "xn--e1afmkfd.xn--p1ai"), ("WWW.Straße.de", "www.xn--strae-oqa.de")])
def test_names_are_normalized_as_in_paste(text: str, name: str) -> None:
    assert domains(text) == (domain(1, name),)


def test_lines_before_first_header_are_ungrouped_and_ungrouped_resets_the_group() -> None:
    text = 'a.com\n[group "G"]\nb.com\n[ungrouped]\nc.com\n'
    assert domains(text) == (domain(1, "a.com"), domain(3, "b.com", groups=("G",)), domain(5, "c.com"))


def test_extra_groups_follow_the_section_group_without_repeats() -> None:
    text = '[group "Video"]\nyoutube.com @Google @Video @Media @Google\n[ungrouped]\nexample.com @Media @Google\n'
    assert domains(text) == (domain(2, "youtube.com", groups=("Video", "Google", "Media")), domain(4, "example.com", groups=("Media", "Google")))


def test_group_and_tag_names_can_be_quoted() -> None:
    text = '[group "example.com +2"]\na.com @"x \\"y\\" \\\\ z" #"two words" #"semi;colon"\n'
    assert parse_import(text) == ParseResult(
        ImportFile(groups=(GroupSection(1, "example.com +2"),), domains=(domain(2, "a.com", groups=("example.com +2", 'x "y" \\ z'), tags=("two words", "semi;colon")),))
    )


def test_tags_keep_order_without_repeats() -> None:
    assert domains("a.com #b #a #b") == (domain(1, "a.com", tags=("b", "a")),)


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ("/8", {"prefix_v4": 8}),
        ("/32", {"prefix_v4": 32}),
        ("v6/16", {"prefix_v6": 16}),
        ("v6/128", {"prefix_v6": 128}),
        ("v6/40 /22", {"prefix_v4": 22, "prefix_v6": 40}),
        ("!disabled !disabled", {"enabled": False}),
        ("!disabled #t @G /24 ; c", {"enabled": False, "tags": ("t",), "groups": ("G",), "prefix_v4": 24, "comment": "c"}),
        (";comment;with ; semicolons ", {"comment": "comment;with ; semicolons"}),
        (";", {}),
    ],
)
def test_domain_attributes(attrs: str, expected: dict[str, object]) -> None:
    assert domains(f"example.com {attrs}") == (domain(1, "example.com", **expected),)


def test_comment_may_follow_the_name_without_space() -> None:
    assert domains("example.com;note") == (domain(1, "example.com", comment="note"),)


@pytest.mark.parametrize(
    "line",
    [
        "example.com /7",
        "example.com /33",
        "example.com /x",
        "example.com /",
        "example.com /\uff12\uff14",
        "example.com v6/15",
        "example.com v6/129",
        "example.com /24 /24",
        "example.com v6/48 v6/40",
        "example.com !enabled",
        "example.com unexpected",
        "example.com #",
        "example.com @",
        'example.com @""',
        'example.com #""',
        "example.com #" + "t" * 51,
        "example.com @" + "g" * 101,
        'example.com @"unterminated',
        'example.com @"a"b',
        'example.com "quoted"',
        "@G example.com",
        "*.example.com",
        "localhost",
        "1.2.3.4",
        "exa_mple.com",
        "a" * 64 + ".com",
    ],
)
def test_invalid_domain_line_is_an_error_with_its_line(line: str) -> None:
    result = parse_import(f"ok.com\n{line}\nalso-ok.com")
    assert [e.line for e in result.errors] == [2]
    assert result.value.domains == (domain(1, "ok.com"), domain(3, "also-ok.com"))


def test_longest_tag_and_group_names_are_accepted() -> None:
    assert domains(f"a.com #{'t' * 50} @{'g' * 100}") == (domain(1, "a.com", tags=("t" * 50,), groups=("g" * 100,)),)


def test_every_error_on_a_line_is_reported() -> None:
    assert error_lines("example.com /7 v6/7 !nope") == [1, 1, 1]


def test_group_properties() -> None:
    text = '[group "G"]\ndescription =  Video; hosting #1 @x  \nprefix_v4=8\nprefix_v6 = 128\nenabled = false\n\n# comment\na.com\n[group "H"]\nenabled = true\ndescription =\n'
    assert parse_import(text) == ParseResult(
        ImportFile(
            groups=(GroupSection(1, "G", description="Video; hosting #1 @x", prefix_v4=8, prefix_v6=128, enabled=False), GroupSection(9, "H", description="", enabled=True)),
            domains=(domain(8, "a.com", groups=("G",)),),
        )
    )


def test_group_without_domains_is_listed_and_groups_from_at_are_not() -> None:
    assert parse_import('[group "Empty"]\n[ungrouped]\na.com @Other\n').value.groups == (GroupSection(1, "Empty"),)


@pytest.mark.parametrize(
    "lines",
    [
        "colour = red",
        "prefix_v4 = 7",
        "prefix_v4 = 33",
        "prefix_v4 = x",
        "prefix_v6 = 15",
        "prefix_v6 = 129",
        "enabled = yes",
        "enabled = False",
        "description = a\ndescription = b",
        "prefix_v4 = 24\nprefix_v4 = 24",
    ],
)
def test_invalid_group_property_is_an_error(lines: str) -> None:
    text = f'[group "G"]\n{lines}\na.com\n'
    assert error_lines(text) == [lines.count("\n") + 2]
    assert parse_import(text).value.domains == (domain(lines.count("\n") + 3, "a.com", groups=("G",)),)


def test_group_property_after_a_domain_line_is_an_error() -> None:
    assert error_lines('[group "G"]\na.com\nprefix_v4 = 24\n') == [3]


@pytest.mark.parametrize("text", ["prefix_v4 = 24\na.com", "[ungrouped]\nprefix_v4 = 24\na.com"])
def test_property_outside_a_group_is_an_error(text: str) -> None:
    assert error_lines(text) == [text.count("\n")]


def test_repeated_group_section_is_an_error() -> None:
    result = parse_import('[group "G"]\na.com\n[group "H"]\n[group "G"]\nb.com\n')
    assert [e.line for e in result.errors] == [4]
    assert result.value.groups == (GroupSection(1, "G"), GroupSection(3, "H"))


def test_settings() -> None:
    text = '[settings]\nresolve.interval = 60m\n\n# comment\ndiscovery.ignore = ["w3.org", "schema.org"] ; not a comment\nroutes.exclude=[]\n'
    assert parse_import(text).value.settings == (
        SettingLine(2, "resolve.interval", "60m"),
        SettingLine(5, "discovery.ignore", '["w3.org", "schema.org"] ; not a comment'),
        SettingLine(6, "routes.exclude", "[]"),
    )


@pytest.mark.parametrize(("lines", "bad"), [("a.com", 2), ("resolve.interval = 60m\nresolve.interval = 30m", 3), ("= 5", 2), ("resolve.interval", 2)])
def test_invalid_settings_line_is_an_error(lines: str, bad: int) -> None:
    assert error_lines(f"[settings]\n{lines}\n") == [bad]


def test_routes() -> None:
    text = "[routes]\n1.2.3.4\n10.1.2.3/8 @A @B @A ; private\n2001:db8::1/32 !disabled\n2001:db8:1::5\n"
    assert parse_import(text).value.routes == (
        RouteLine(2, ip_network("1.2.3.4/32")),
        RouteLine(3, ip_network("10.0.0.0/8"), groups=("A", "B"), comment="private"),
        RouteLine(4, ip_network("2001:db8::/32"), enabled=False),
        RouteLine(5, ip_network("2001:db8:1::5/128")),
    )


@pytest.mark.parametrize(
    "line",
    [
        "example.com",
        "1.2.3.0/33",
        "2001:db8::/129",
        "fe80::1%eth0",
        "::ffff:1.2.3.4",
        "::ffff:1.2.3.0/120",
        "1.2.3.0/24 /24",
        "1.2.3.0/24 v6/48",
        "1.2.3.0/24 #tag",
        "1.2.3.0/24 !nope",
        "1.2.3.0/24 extra",
        "@A 1.2.3.0/24",
    ],
)
def test_invalid_route_line_is_an_error(line: str) -> None:
    result = parse_import(f"[routes]\n10.0.0.0/8\n{line}\n")
    assert [e.line for e in result.errors] == [3]
    assert result.value.routes == (RouteLine(2, ip_network("10.0.0.0/8")),)


def test_ipv4_mapped_route_is_an_error_but_a_covering_network_is_not() -> None:
    result = parse_import("[routes]\n::ffff:1.2.3.4\n::/0\n")
    assert [(e.line, e.message) for e in result.errors] == [(2, "'::ffff:1.2.3.4': IPv4-mapped addresses are not routable, use the IPv4 form")]
    assert result.value.routes == (RouteLine(3, ip_network("::/0")),)


def test_repeated_route_network_is_an_error() -> None:
    assert error_lines("[routes]\n1.2.3.0/24\n1.2.3.4/24\n") == [3]


def test_addresses_section_is_skipped() -> None:
    text = "a.com\n[addresses]\na.com 1.2.3.4 system 1760000000 1760000100\nanything at all\n[ungrouped]\nb.com\n"
    assert parse_import(text) == ParseResult(ImportFile(domains=(domain(1, "a.com"), domain(6, "b.com"))))


@pytest.mark.parametrize("header", ["[unknown]", "[group Video]", '[group ""]', f'[group "{"g" * 101}"]', "[group]", "[routes", "[Settings]", '[group "a"b"]'])
def test_unknown_or_malformed_header_is_an_error_and_its_lines_are_skipped(header: str) -> None:
    result = parse_import(f"a.com\n{header}\nnot a domain line\nb.com\n[ungrouped]\nc.com\n")
    assert [e.line for e in result.errors] == [2]
    assert result.value == ImportFile(domains=(domain(1, "a.com"), domain(6, "c.com")))


@pytest.mark.parametrize("header", ["# subnet-tracker export v2", "  #subnet-tracker export v0"])
def test_unsupported_format_version_is_an_error(header: str) -> None:
    assert error_lines(f"{header}\na.com\n") == [1]


@pytest.mark.parametrize(
    ("text", "kept", "duplicates"),
    [
        ("example.com\nwww.example.com", [domain(1, "example.com")], [DuplicateLine(2, DomainName("www.example.com"), 1)]),
        ("www.example.net\nexample.net /16 #t", [domain(1, "www.example.net")], [DuplicateLine(2, DomainName("example.net"), 1)]),
        ("a.com\nb.com\nA.com. @G\n", [domain(1, "a.com"), domain(2, "b.com")], [DuplicateLine(3, DomainName("a.com"), 1)]),
        ("www.a.com\n\nwww.www.a.com\n", [domain(1, "www.a.com")], [DuplicateLine(3, DomainName("www.www.a.com"), 1)]),
        ('[group "G"]\na.com\n[group "H"]\nwww.a.com\n', [domain(2, "a.com", groups=("G",))], [DuplicateLine(4, DomainName("www.a.com"), 2)]),
    ],
)
def test_duplicate_line_is_a_warning_with_line_numbers(text: str, kept: list[DomainLine], duplicates: list[DuplicateLine]) -> None:
    result = parse_import(text)
    assert result.ok
    assert result.value.domains == tuple(kept)
    assert result.value.duplicates == tuple(duplicates)


def test_all_errors_are_collected_with_line_numbers() -> None:
    text = 'bad\n[settings]\nnope\n[group "G"]\ncolour = red\na.com /99\n[routes]\n1.2.3.0/33\n[wat]\nignored\n[ungrouped]\n*.b.com\n'
    assert error_lines(text) == [1, 3, 5, 6, 8, 9, 12]


def test_error_message_quotes_the_offending_token() -> None:
    (error,) = parse_import("example.com /99").errors
    assert "'/99'" in error.message
