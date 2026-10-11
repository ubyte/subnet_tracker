import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum

from subnet_tracker.types import FieldError, JsonValue, Timestamp

PROG = "subnet-tracker"


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    PARTIAL = 3


@dataclass(frozen=True, slots=True)
class Done:
    text: str
    data: JsonValue
    code: ExitCode = ExitCode.OK
    # the command changed data: tick is notified once its transactions are committed
    notify: bool = False


@dataclass(frozen=True, slots=True)
class Failed:
    code: ExitCode
    messages: tuple[str, ...]


type Outcome = Done | Failed


@dataclass(frozen=True, slots=True)
class Rendered:
    stdout: str
    stderr: str


def table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = [headers, *rows]
    widths = [max(map(len, column)) for column in zip(*lines, strict=True)]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip() for line in lines)


def fields(pairs: Sequence[tuple[str, str]]) -> str:
    width = max(len(key) for key, _ in pairs)
    return "\n".join(f"{key.ljust(width)}  {value}".rstrip() for key, value in pairs)


def listed(items: Sequence[str]) -> str:
    return ", ".join(items) or "-"


def moment(ts: Timestamp | None) -> str:
    return "-" if ts is None else time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts))


def rejected(error: FieldError) -> Failed:
    return Failed(ExitCode.USAGE, (f"{error.field}: {error.message}",))


def changed_line(title: str, changes: Mapping[str, tuple[JsonValue, JsonValue]]) -> str:
    if not changes:
        return "nothing changed"
    parts = [f"{name} {json.dumps(old, ensure_ascii=False)} -> {json.dumps(new, ensure_ascii=False)}" for name, (old, new) in changes.items()]
    return f"{title}: {', '.join(parts)}"


def warning(message: str) -> str:
    return f"{PROG}: warning: {message}\n"


def render(outcome: Outcome, as_json: bool) -> Rendered:
    match outcome:
        case Failed(_, messages):
            return Rendered("", "".join(f"{PROG}: error: {message}\n" for message in messages))
        case Done(_, data) if as_json:
            return Rendered(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "")
        case Done(text):
            return Rendered(f"{text}\n" if text else "", "")
