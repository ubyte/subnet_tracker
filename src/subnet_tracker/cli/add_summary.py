from collections.abc import Sequence

from subnet_tracker.cli.output import PROG, Done, ExitCode, Outcome, rejected
from subnet_tracker.core.batches import AddResult
from subnet_tracker.types import FieldError, JsonValue, ParseError

# larger lists are only counted, as in the web summary (9.3)
NAMED_LIMIT = 50


def counted(head: str, names: Sequence[str]) -> str:
    return head if not names or len(names) > NAMED_LIMIT else f"{head}: {', '.join(names)}"


def invalid_text(errors: Sequence[ParseError]) -> list[str]:
    return [f"not recognized: line {e.line}: {e.message}" for e in errors]


def invalid_json(errors: Sequence[ParseError]) -> JsonValue:
    return [{"line": e.line, "message": e.message} for e in errors]


def summary_text(result: AddResult) -> str:
    lines = [counted(f"added {len(result.added)} in batch {result.batch_id}", [d.canonical for d in result.added])]
    if result.duplicates:
        lines.append(counted(f"skipped {len(result.duplicates)} already tracked", result.duplicates))
    lines += invalid_text(result.invalid)
    if result.networks:
        lines.append(f"networks, add them with '{PROG} route add': {', '.join(map(str, result.networks))}")
    if result.wildcards:
        lines.append(f"wildcards are not supported, added without '*.': {', '.join(result.wildcards)}")
    return "\n".join(lines)


def summary_json(result: AddResult) -> JsonValue:
    return {
        "batch_id": result.batch_id,
        "added": [d.canonical for d in result.added],
        "duplicates": list(result.duplicates),
        "invalid": invalid_json(result.invalid),
        "networks": [str(n) for n in result.networks],
        "wildcards": list(result.wildcards),
    }


# unrecognized input is a partial success: the rest is added and the batch recorded
def add_outcome(result: AddResult | FieldError) -> Outcome:
    if isinstance(result, FieldError):
        return rejected(result)
    return Done(summary_text(result), summary_json(result), ExitCode.PARTIAL if result.invalid else ExitCode.OK, notify=bool(result.added))
