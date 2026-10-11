import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum

from subnet_tracker.types import JsonValue

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
