from hypothesis import given
from hypothesis import strategies as st

from subnet_tracker.discovery.www import domain_names
from subnet_tracker.textio.changes import ImportDiff, ImportMode
from subnet_tracker.textio.diff import import_diff
from subnet_tracker.textio.export import render_export
from subnet_tracker.textio.model import ExportFile, ImportFile
from subnet_tracker.textio.parse import parse_import
from textio_apply import apply_diff, apply_domains
from textio_strategies import related_states, stored_states


def _text(state: ExportFile) -> str:
    text = render_export(state)
    assert isinstance(text, str), text
    return text


def _file(state: ExportFile) -> ImportFile:
    parsed = parse_import(_text(state))
    assert parsed.errors == ()
    return parsed.value


def _diff(current: ExportFile, file: ImportFile, mode: ImportMode) -> ImportDiff:
    result = import_diff(current, file, mode)
    assert result.errors == ()
    return result.value


@st.composite
def _source_and_current(draw: st.DrawFn) -> tuple[ExportFile, ExportFile]:
    source = draw(stored_states())
    return source, draw(st.just(ExportFile()) | related_states(source))


@given(_source_and_current())
def test_replace_with_an_export_gives_the_same_export_and_keeps_domains_of_the_same_pair(states: tuple[ExportFile, ExportFile]) -> None:
    source, current = states
    diff = _diff(current, _file(source), ImportMode.REPLACE)
    replaced = apply_diff(current, diff)
    assert _text(replaced) == _text(source)
    assert _diff(replaced, _file(source), ImportMode.MERGE) == ImportDiff()
    pairs = {frozenset(domain_names(d.name).all) for d in source.domains}
    before = dict(enumerate(current.domains, 1))
    after = apply_domains(before, diff.domains)
    assert all(i in after for i, d in before.items() if frozenset(domain_names(d.name).all) in pairs)


@given(stored_states())
def test_merge_of_the_own_export_changes_nothing(state: ExportFile) -> None:
    assert _diff(state, _file(state), ImportMode.MERGE) == ImportDiff()


@given(_source_and_current())
def test_merge_removes_nothing_covers_every_line_and_is_idempotent(states: tuple[ExportFile, ExportFile]) -> None:
    source, current = states
    file = _file(source)
    diff = _diff(current, file, ImportMode.MERGE)
    assert (diff.groups.removed, diff.domains.removed, diff.routes.removed) == ((), (), ())
    merged = apply_diff(current, diff)
    kept = {d.name: d for d in merged.domains}
    assert all(kept[d.name].groups >= d.groups and kept[d.name].tags >= d.tags for d in current.domains)
    owners = {n: d for d in merged.domains for n in domain_names(d.name).all}
    skipped = {dup.line for dup in diff.duplicates}
    for line in file.domains:
        owner = next(owners[n] for n in domain_names(line.name).all if n in owners)
        assert line.line in skipped or (owner.groups >= set(line.groups) and owner.tags >= set(line.tags))
    assert _diff(merged, file, ImportMode.MERGE) == ImportDiff(duplicates=diff.duplicates)
