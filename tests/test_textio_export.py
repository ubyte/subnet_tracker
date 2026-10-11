import re
from dataclasses import replace
from ipaddress import ip_address, ip_network
from pathlib import Path
from random import Random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from subnet_tracker.core.settings import SettingValue, parse_setting
from subnet_tracker.discovery.parse import Network
from subnet_tracker.discovery.www import domain_names
from subnet_tracker.textio.export import LineBreaks, render_export
from subnet_tracker.textio.lines import MAX_GROUP_NAME, MAX_TAG_NAME
from subnet_tracker.textio.model import ExportAddress, ExportDomain, ExportFile, ExportGroup, ExportRoute, ExportSetting, ImportFile, SettingLine
from subnet_tracker.textio.parse import parse_import
from subnet_tracker.types import PREFIX_V4, PREFIX_V6, DomainName, Timestamp
from textio_strategies import LINE_BREAKS, one_line_text, trimmed_text

HEADER = "# subnet-tracker export v1\n"
T0 = Timestamp(1791688800)


def domain(name: str, **attrs: object) -> ExportDomain:
    return ExportDomain(DomainName(name), **attrs)  # type: ignore[arg-type]


def test_empty_model_renders_only_the_header() -> None:
    assert render_export(ExportFile()) == HEADER


def test_sections_properties_and_attributes() -> None:
    model = ExportFile(
        settings=(ExportSetting("routes.default_prefix_v4", "23"), ExportSetting("resolve.interval", "60m")),
        groups=(ExportGroup("Video", description="Video hosting", prefix_v4=24), ExportGroup("Messengers"), ExportGroup("Google"), ExportGroup("Empty", prefix_v6=40, enabled=False)),
        domains=(
            domain("ytimg.com", groups=frozenset({"Video"}), tags=frozenset({"video"}), comment="static content"),
            domain("www.example.net", comment="canonical name with www, pair example.net"),
            domain("youtube.com", groups=frozenset({"Video"}), tags=frozenset({"video", "google"})),
            domain("googlevideo.com", groups=frozenset({"Video", "Google"}), tags=frozenset({"video"}), prefix_v4=16),
            domain("example.org", prefix_v4=22, prefix_v6=40, enabled=False),
        ),
        routes=(
            ExportRoute(ip_network("2001:db8::/32"), enabled=False),
            ExportRoute(ip_network("192.168.10.0/24"), comment="LAN behind the server"),
            ExportRoute(ip_network("149.154.160.0/20"), groups=frozenset({"Messengers", "Google"}), comment="Telegram ranges"),
        ),
    )
    assert render_export(model) == HEADER + (
        "\n"
        "[settings]\n"
        "resolve.interval = 60m\n"
        "routes.default_prefix_v4 = 23\n"
        "\n"
        '[group "Empty"]\n'
        "prefix_v6 = 40\n"
        "enabled = false\n"
        "\n"
        '[group "Google"]\n'
        "googlevideo.com /16 #video @Video\n"
        "\n"
        '[group "Messengers"]\n'
        "\n"
        '[group "Video"]\n'
        "description = Video hosting\n"
        "prefix_v4 = 24\n"
        "\n"
        "youtube.com #google #video\n"
        "ytimg.com #video ; static content\n"
        "\n"
        "[ungrouped]\n"
        "example.org /22 v6/40 !disabled\n"
        "www.example.net ; canonical name with www, pair example.net\n"
        "\n"
        "[routes]\n"
        "149.154.160.0/20 @Google @Messengers ; Telegram ranges\n"
        "192.168.10.0/24 ; LAN behind the server\n"
        "2001:db8::/32 !disabled\n"
    )


def test_names_that_are_not_bare_words_are_quoted() -> None:
    model = ExportFile(
        groups=(ExportGroup("example.com +2"), ExportGroup('say "hi"')),
        domains=(domain("a.com", groups=frozenset({"example.com +2", 'say "hi"', "x\\y"}), tags=frozenset({"two words", "semi;colon", "a\u00a0b", "#hash", "@at"})),),
        routes=(ExportRoute(ip_network("10.0.0.0/8"), groups=frozenset({"x y"})),),
    )
    assert render_export(model) == HEADER + (
        '\n[group "example.com +2"]\na.com ##hash #@at #"a\u00a0b" #"semi;colon" #"two words" @"say \\"hi\\"" @x\\y\n\n[group "say \\"hi\\""]\n\n[routes]\n10.0.0.0/8 @"x y"\n'
    )


def test_addresses_are_rendered_only_when_given() -> None:
    model = ExportFile(domains=(domain("example.com"),))
    with_addresses = replace(
        model,
        addresses=(
            ExportAddress(DomainName("www.example.com"), ip_address("2001:db8::1"), "system", T0, T0),
            ExportAddress(DomainName("example.com"), ip_address("10.0.0.2"), "system", T0, Timestamp(T0 + 300)),
            ExportAddress(DomainName("example.com"), ip_address("9.0.0.1"), "system", T0, T0),
            ExportAddress(DomainName("example.com"), ip_address("9.0.0.1"), "Cloudflare DoH", Timestamp(T0 - 86400), T0),
        ),
    )
    assert render_export(model) == HEADER + "\n[ungrouped]\nexample.com\n"
    assert render_export(with_addresses) == HEADER + (
        "\n"
        "[ungrouped]\n"
        "example.com\n"
        "\n"
        "[addresses]\n"
        'example.com 9.0.0.1 "Cloudflare DoH" 2026-10-10T03:20:00Z 2026-10-11T03:20:00Z\n'
        "example.com 9.0.0.1 system 2026-10-11T03:20:00Z 2026-10-11T03:20:00Z\n"
        "example.com 10.0.0.2 system 2026-10-11T03:20:00Z 2026-10-11T03:25:00Z\n"
        "www.example.com 2001:db8::1 system 2026-10-11T03:20:00Z 2026-10-11T03:20:00Z\n"
    )
    assert render_export(ExportFile(addresses=())) == HEADER + "\n[addresses]\n"


_NET = ip_network("10.0.0.0/8")
_IP = ip_address("9.0.0.1")


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (ExportFile(settings=(ExportSetting("a.b", "x\ny"),)), "setting 'a.b' value"),
        (ExportFile(settings=(ExportSetting("a\rb", "x"),)), "setting 'a\\rb'"),
        (ExportFile(groups=(ExportGroup("G\x85"),)), "group 'G\\x85'"),
        (ExportFile(groups=(ExportGroup("G", description="a\u2028b"),)), "group 'G' description"),
        (ExportFile(domains=(domain("a.com\n[routes]"),)), "domain 'a.com\\n[routes]'"),
        (ExportFile(domains=(domain("a.com", groups=frozenset({"G\x0b"})),)), "domain 'a.com' group 'G\\x0b'"),
        (ExportFile(domains=(domain("a.com", tags=frozenset({"t\x1c"})),)), "domain 'a.com' tag 't\\x1c'"),
        (ExportFile(domains=(domain("a.com", comment="a\r\nb"),)), "domain 'a.com' comment"),
        (ExportFile(routes=(ExportRoute(_NET, groups=frozenset({"G\u2029"})),)), "route 10.0.0.0/8 group 'G\\u2029'"),
        (ExportFile(routes=(ExportRoute(_NET, comment="c\x0c"),)), "route 10.0.0.0/8 comment"),
        (ExportFile(addresses=(ExportAddress(DomainName("a.com\x1d"), _IP, "system", T0, T0),)), "address 'a.com\\x1d'"),
        (ExportFile(addresses=(ExportAddress(DomainName("a.com"), _IP, "x\n[routes]\n0.0.0.0/1", T0, T0),)), "address 'a.com' 9.0.0.1 resolver 'x\\n[routes]\\n0.0.0.0/1'"),
    ],
)
def test_field_with_a_line_break_is_refused_by_name(model: ExportFile, field: str) -> None:
    assert render_export(model) == LineBreaks((field,))


@pytest.mark.parametrize("brk", LINE_BREAKS)
def test_every_line_break_is_refused(brk: str) -> None:
    assert render_export(ExportFile(domains=(domain("a.com", comment=f"a{brk}"),))) == LineBreaks(("domain 'a.com' comment",))


def test_all_fields_with_line_breaks_are_listed_in_order() -> None:
    model = ExportFile(groups=(ExportGroup("G", description="\n"),), domains=(domain("b.com", tags=frozenset({"y\n", "x\n"}), comment="\n"), domain("a.com", comment="\n")))
    assert render_export(model) == LineBreaks(("domain 'a.com' comment", "domain 'b.com' comment", "domain 'b.com' tag 'x\\n'", "domain 'b.com' tag 'y\\n'", "group 'G' description"))


def _optional(allowed: range) -> st.SearchStrategy[int | None]:
    return st.none() | st.integers(allowed.start, allowed.stop - 1)


def _subsets(names: list[str]) -> st.SearchStrategy[frozenset[str]]:
    return st.frozensets(st.sampled_from(names), max_size=3) if names else st.just(frozenset())


def _distinct_pairs(domains: list[ExportDomain]) -> tuple[ExportDomain, ...]:
    seen: set[DomainName] = set()
    kept = []
    for d in domains:
        names = domain_names(d.name).all
        if seen.isdisjoint(names):
            kept.append(d)
            seen.update(names)
    return tuple(kept)


def _network(address: st.SearchStrategy[str], bits: int) -> st.SearchStrategy[Network]:
    return st.builds(lambda a, p: ip_network(f"{a}/{p}", strict=False), address, st.integers(0, bits))


_labels = st.lists(st.from_regex(r"[a-z0-9]{1,8}", fullmatch=True), min_size=1, max_size=3)
_domain_name = st.builds(lambda www, labels, tld: DomainName(www + ".".join([*labels, tld])), st.sampled_from(["", "www.", "www.www."]), _labels, st.from_regex(r"[a-z]{2,6}", fullmatch=True))
_timestamps = st.integers(0, 2**33).map(Timestamp)
_networks = _network(st.ip_addresses(v=4).map(str), 32) | _network(st.ip_addresses(v=6).map(str), 128)
_settings = st.lists(st.builds(ExportSetting, st.from_regex(r"[a-z][a-z0-9_]{0,8}(\.[a-z0-9_]{1,8}){0,2}", fullmatch=True), trimmed_text()), max_size=3, unique_by=lambda s: s.key)
_groups = st.lists(st.builds(ExportGroup, one_line_text(1, MAX_GROUP_NAME), trimmed_text(), _optional(PREFIX_V4), _optional(PREFIX_V6), st.booleans()), max_size=4, unique_by=lambda g: g.name)
_tags = st.frozensets(one_line_text(1, MAX_TAG_NAME), max_size=3)
_addresses = st.none() | st.lists(st.builds(ExportAddress, _domain_name, st.ip_addresses(), one_line_text(1, 100), _timestamps, _timestamps), max_size=3).map(tuple)


@st.composite
def models(draw: st.DrawFn) -> ExportFile:
    groups = draw(_groups)
    in_groups = _subsets([g.name for g in groups])
    domains = draw(st.lists(st.builds(ExportDomain, _domain_name, in_groups, _tags, _optional(PREFIX_V4), _optional(PREFIX_V6), st.booleans(), trimmed_text()), max_size=8).map(_distinct_pairs))
    routes = draw(st.lists(st.builds(ExportRoute, _networks, in_groups, st.booleans(), trimmed_text()), max_size=4, unique_by=lambda r: r.network))
    return ExportFile(tuple(draw(_settings)), tuple(groups), domains, tuple(routes), draw(_addresses))


def _exported(parsed: ImportFile) -> ExportFile:
    return ExportFile(
        tuple(ExportSetting(s.key, s.value) for s in parsed.settings),
        tuple(ExportGroup(g.name, g.description or "", g.prefix_v4, g.prefix_v6, g.enabled is not False) for g in parsed.groups),
        tuple(ExportDomain(d.name, frozenset(d.groups), frozenset(d.tags), d.prefix_v4, d.prefix_v6, d.enabled, d.comment) for d in parsed.domains),
        tuple(ExportRoute(r.network, frozenset(r.groups), r.enabled, r.comment) for r in parsed.routes),
    )


def _contents(model: ExportFile) -> tuple[frozenset[object], ...]:
    return frozenset(model.settings), frozenset(model.groups), frozenset(model.domains), frozenset(model.routes)


def _rendered(model: ExportFile) -> str:
    text = render_export(model)
    assert isinstance(text, str), text
    return text


@given(models())
def test_parse_import_reads_the_export_back(model: ExportFile) -> None:
    parsed = parse_import(_rendered(model))
    assert parsed.errors == ()
    assert parsed.value.duplicates == ()
    assert _contents(_exported(parsed.value)) == _contents(model)


def _stored_setting(line: SettingLine) -> ExportSetting | None:
    value = parse_setting(line.key, line.value)
    assert isinstance(value, SettingValue), value
    return None if value.is_default else ExportSetting(value.key, value.text)


def _as_stored(parsed: ImportFile) -> ExportFile:
    # what import stores and the CLI export reads back: defaults are never stored, groups come from sections and @ references
    exported = _exported(parsed)
    referenced = {n for d in exported.domains for n in d.groups} | {n for r in exported.routes for n in r.groups}
    referenced -= {g.name for g in exported.groups}
    settings = tuple(s for line in parsed.settings if (s := _stored_setting(line)))
    return replace(exported, settings=settings, groups=exported.groups + tuple(ExportGroup(name) for name in referenced))


def _design_examples() -> list[str]:
    design = (Path(__file__).parents[1] / "docs" / "design.md").read_text(encoding="utf-8")
    section = design[design.index("\n## 12. ") : design.index("\n## 13. ")]
    return re.findall(r"^```\n(.*?)^```$", section, re.DOTALL | re.MULTILINE)


def test_hand_written_design_example_exports_as_the_literal_one() -> None:
    hand_written, literal = _design_examples()
    parsed = parse_import(hand_written)
    assert parsed.errors == ()
    assert parsed.value.duplicates == ()
    assert render_export(_as_stored(parsed.value)) == literal


@given(models(), st.randoms(use_true_random=False))
def test_output_does_not_depend_on_input_order(model: ExportFile, random: Random) -> None:
    def shuffled[T](items: tuple[T, ...]) -> tuple[T, ...]:
        return tuple(random.sample(items, len(items)))

    reordered = ExportFile(shuffled(model.settings), shuffled(model.groups), shuffled(model.domains), shuffled(model.routes), None if model.addresses is None else shuffled(model.addresses))
    assert render_export(reordered) == render_export(model)
