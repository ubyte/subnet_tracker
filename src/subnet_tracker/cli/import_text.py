from subnet_tracker.textio.changes import Counts, ImportDiff, import_summary
from subnet_tracker.types import JsonValue


def _counts(label: str, counts: Counts, noun: str) -> str:
    return f"{label}: {counts.added} added, {counts.updated} updated, {counts.removed} {noun}"


def has_changes(diff: ImportDiff) -> bool:
    summary = import_summary(diff)
    parts = (summary.settings, summary.groups, summary.domains, summary.routes)
    return any(counts.added or counts.updated or counts.removed for counts in parts)


def format_summary(diff: ImportDiff) -> str:
    summary = import_summary(diff)
    parts = (summary.settings, summary.groups, summary.domains, summary.routes)
    if not any(counts.added or counts.updated or counts.removed for counts in parts) and not summary.duplicates:
        return "nothing changed"
    domains = _counts("domains", summary.domains, "removed")
    if summary.canonical:
        domains += f" ({summary.canonical} canonical)"
    lines = [_counts("settings", summary.settings, "reset"), _counts("groups", summary.groups, "removed"), domains, _counts("routes", summary.routes, "removed")]
    if summary.duplicates:
        lines.append(f"duplicates: {summary.duplicates}")
    return "\n".join(lines)


def duplicate_warnings(diff: ImportDiff) -> tuple[str, ...]:
    return tuple(f"line {dup.line}: {dup.name} repeats line {dup.first_line}" for dup in diff.duplicates)


def _count_json(counts: Counts) -> dict[str, int]:
    return {"added": counts.added, "updated": counts.updated, "removed": counts.removed}


def import_data(diff: ImportDiff, applied: bool) -> JsonValue:
    summary = import_summary(diff)
    return {
        "summary": {
            "settings": _count_json(summary.settings),
            "groups": _count_json(summary.groups),
            "domains": _count_json(summary.domains),
            "canonical": summary.canonical,
            "routes": _count_json(summary.routes),
            "duplicates": summary.duplicates,
        },
        "applied": applied,
        "duplicates": [{"line": dup.line, "name": dup.name, "first": dup.first_line} for dup in diff.duplicates],
    }
