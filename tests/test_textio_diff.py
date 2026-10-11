from ipaddress import ip_network

import pytest

from subnet_tracker.core.settings import SettingValue, parse_setting
from subnet_tracker.textio.changes import Changes, Counts, ImportDiff, ImportMode, ImportSummary, SettingChange, Update, import_summary
from subnet_tracker.textio.diff import import_diff
from subnet_tracker.textio.model import DuplicateLine, ExportDomain, ExportFile, ExportGroup, ExportRoute, ExportSetting, ImportFile
from subnet_tracker.textio.parse import parse_import
from subnet_tracker.types import DomainName, ParseError

MERGE, REPLACE = ImportMode.MERGE, ImportMode.REPLACE
NET = ip_network("10.0.0.0/8")


def domain(name: str, **attrs: object) -> ExportDomain:
    return ExportDomain(DomainName(name), **attrs)  # type: ignore[arg-type]


def value(key: str, text: str) -> SettingValue:
    parsed = parse_setting(key, text)
    assert isinstance(parsed, SettingValue), parsed
    return parsed


def parsed(text: str) -> ImportFile:
    result = parse_import(text)
    assert result.errors == ()
    return result.value


def diff(current: ExportFile, text: str, mode: ImportMode) -> ImportDiff:
    result = import_diff(current, parsed(text), mode)
    assert result.errors == ()
    return result.value


FILE = """
[settings]
resolve.interval = 2h
routes.default_prefix_v4 = 23

[group "Video"]
description = Video hosting
prefix_v4 = 24

youtube.com #video @Google

[ungrouped]
www.example.net /22 !disabled ; note

[routes]
10.0.0.0/8 @Messengers ; lan
"""


@pytest.mark.parametrize("mode", list(ImportMode))
def test_import_into_an_empty_state_adds_everything_and_referenced_groups(mode: ImportMode) -> None:
    assert diff(ExportFile(), FILE, mode) == ImportDiff(
        settings=(SettingChange("resolve.interval", None, value("resolve.interval", "2h")),),
        groups=Changes(added=(ExportGroup("Google"), ExportGroup("Messengers"), ExportGroup("Video", "Video hosting", 24))),
        domains=Changes(added=(domain("www.example.net", prefix_v4=22, enabled=False, comment="note"), domain("youtube.com", groups=frozenset({"Video", "Google"}), tags=frozenset({"video"})))),
        routes=Changes(added=(ExportRoute(NET, frozenset({"Messengers"}), comment="lan"),)),
    )


OLD = domain("example.com", groups=frozenset({"A"}), tags=frozenset({"t"}), prefix_v4=20, prefix_v6=40, enabled=False, comment="kept")
WITH_OLD = ExportFile(groups=(ExportGroup("A"),), domains=(OLD,))


def test_merge_matches_a_domain_by_its_pair_and_keeps_its_canonical_name() -> None:
    assert diff(WITH_OLD, "www.example.com #u @B\n", MERGE) == ImportDiff(
        groups=Changes(added=(ExportGroup("B"),)),
        domains=Changes(updated=(Update(OLD, domain("example.com", groups=frozenset({"A", "B"}), tags=frozenset({"t", "u"}), prefix_v4=20, prefix_v6=40, enabled=False, comment="kept")),)),
    )


def test_merge_applies_prefixes_disabled_and_comment_only_when_written() -> None:
    current = ExportFile(domains=(domain("a.com", prefix_v4=20, prefix_v6=40, comment="old"), domain("b.com", prefix_v4=20, comment="old")))
    new = diff(current, "a.com /24 !disabled ; new\nb.com\n", MERGE)
    assert new == ImportDiff(domains=Changes(updated=(Update(current.domains[0], domain("a.com", prefix_v4=24, prefix_v6=40, enabled=False, comment="new")),)))


def test_merge_keeps_what_the_file_does_not_mention() -> None:
    in_g = frozenset({"G"})
    current = ExportFile(settings=(ExportSetting("routes.max_v4", "500"),), groups=(ExportGroup("G", prefix_v4=20),), domains=(domain("b.com", groups=in_g),), routes=(ExportRoute(NET, in_g),))
    assert diff(current, '[group "G"]\n\n[ungrouped]\nx.com\n', MERGE) == ImportDiff(domains=Changes(added=(domain("x.com"),)))


GROUP = ExportGroup("G", "desc", 20, 40, False)


@pytest.mark.parametrize(("mode", "new"), [(MERGE, ExportGroup("G", "desc", 24, 40, False)), (REPLACE, ExportGroup("G", prefix_v4=24))])
def test_group_properties_apply_when_written_and_replace_resets_the_rest(mode: ImportMode, new: ExportGroup) -> None:
    assert diff(ExportFile(groups=(GROUP,)), '[group "G"]\nprefix_v4 = 24\n', mode) == ImportDiff(groups=Changes(updated=(Update(GROUP, new),)))


def test_replace_resets_missing_attributes_and_removes_everything_else() -> None:
    route = ExportRoute(NET, frozenset({"H"}), enabled=False, comment="c")
    other_route = ExportRoute(ip_network("11.0.0.0/8"))
    current = ExportFile(
        settings=(ExportSetting("resolve.interval", "2h"), ExportSetting("routes.max_v4", "500")),
        groups=(ExportGroup("A", "desc", 20), ExportGroup("H")),
        domains=(OLD, domain("b.com")),
        routes=(route, other_route),
    )
    assert diff(current, "[settings]\nresolve.interval = 2h\n\n[ungrouped]\nexample.com\n\n[routes]\n10.0.0.0/8 @A\n", REPLACE) == ImportDiff(
        settings=(SettingChange("routes.max_v4", "500", value("routes.max_v4", "1000")),),
        groups=Changes(updated=(Update(ExportGroup("A", "desc", 20), ExportGroup("A")),), removed=(ExportGroup("H"),)),
        domains=Changes(updated=(Update(OLD, domain("example.com")),), removed=(domain("b.com"),)),
        routes=Changes(updated=(Update(route, ExportRoute(NET, frozenset({"A"}))),), removed=(other_route,)),
    )


def test_replace_identifies_domains_by_canonical_name_only() -> None:
    current = ExportFile(domains=(domain("example.com"),))
    assert diff(current, "www.example.com\n", REPLACE) == ImportDiff(domains=Changes(added=(domain("www.example.com"),), removed=(domain("example.com"),)))
    assert diff(current, "www.example.com\n", MERGE) == ImportDiff()


def test_merge_takes_the_first_of_lines_sharing_one_existing_domain() -> None:
    # www.a.com and www.www.www.a.com have distinct name pairs, but both share a name with www.www.a.com
    current = ExportFile(domains=(domain("www.www.a.com"),))
    text = "www.a.com #x\nwww.www.www.a.com #y\n"
    updated = Update(current.domains[0], domain("www.www.a.com", tags=frozenset({"x"})))
    assert diff(current, text, MERGE) == ImportDiff(domains=Changes(updated=(updated,)), duplicates=(DuplicateLine(2, DomainName("www.www.www.a.com"), 1),))
    assert diff(current, text, REPLACE) == ImportDiff(domains=Changes(added=(domain("www.a.com", tags=frozenset({"x"})), domain("www.www.www.a.com", tags=frozenset({"y"}))), removed=current.domains))


def test_duplicates_of_the_file_are_reported_with_the_existing_ones_by_line() -> None:
    current = ExportFile(domains=(domain("www.www.a.com"),))
    result = diff(current, "www.a.com\nb.com\nwww.b.com\nwww.www.www.a.com\n", MERGE)
    assert result.duplicates == (DuplicateLine(3, DomainName("www.b.com"), 2), DuplicateLine(4, DomainName("www.www.www.a.com"), 1))
    assert result.domains == Changes(added=(domain("b.com"),))


def test_invalid_settings_are_errors_with_line_numbers() -> None:
    result = import_diff(ExportFile(), parsed("[settings]\nresolve.interval = soon\nno.such = 1\nresolve.timeout = 10s\n"), MERGE)
    assert result.errors == (ParseError(2, "resolve.interval: invalid duration 'soon', expected <number><s|m|h|d>"), ParseError(3, "unknown setting 'no.such'"))
    assert result.value.settings == (SettingChange("resolve.timeout", None, value("resolve.timeout", "10s")),)


@pytest.mark.parametrize(
    ("stored", "text", "expected"),
    [
        ("90m", "5400s", ()),
        ("90m", "1h", (SettingChange("resolve.interval", "90m", value("resolve.interval", "1h")),)),
        ("90m", "2h", (SettingChange("resolve.interval", "90m", value("resolve.interval", "2h")),)),
        (None, "60m", ()),
    ],
)
@pytest.mark.parametrize("mode", list(ImportMode))
def test_settings_compare_in_registry_text_form_and_defaults_are_not_stored(stored: str | None, text: str, expected: tuple[SettingChange, ...], mode: ImportMode) -> None:
    current = ExportFile(settings=() if stored is None else (ExportSetting("resolve.interval", stored),))
    assert diff(current, f"[settings]\nresolve.interval = {text}\n", mode).settings == expected


def test_summary_counts_every_kind_of_change() -> None:
    current = ExportFile(
        settings=(ExportSetting("resolve.interval", "2h"), ExportSetting("routes.max_v4", "500")),
        groups=(ExportGroup("A", "desc"), ExportGroup("H")),
        domains=(domain("a.com"), domain("b.com"), domain("c.com")),
        routes=(ExportRoute(NET),),
    )
    text = "[settings]\nresolve.interval = 3h\nbrowser.batch = 100\n\n[ungrouped]\na.com !disabled\nx.com\ny.com\nwww.y.com\n\n[routes]\n11.0.0.0/8 @A\n"
    assert import_summary(diff(current, text, REPLACE)) == ImportSummary(settings=Counts(1, 1, 1), groups=Counts(0, 1, 1), domains=Counts(2, 1, 2), routes=Counts(1, 0, 1), duplicates=1)
    assert import_summary(ImportDiff()) == ImportSummary(Counts(), Counts(), Counts(), Counts(), 0)
