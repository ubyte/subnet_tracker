import argparse
from collections.abc import Callable, Sequence

from subnet_tracker.cli.add_summary import add_outcome
from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import ExitCode, Failed, Outcome
from subnet_tracker.core.services.domains import AddOptions, add_pasted
from subnet_tracker.types import BatchKind, ReconnectMode

STDIN = "-"


def input_text(texts: Sequence[str], stdin: Callable[[], str]) -> str | Failed:
    if STDIN in texts and len(texts) > 1:
        return Failed(ExitCode.USAGE, (f"'{STDIN}' reads the text from stdin and goes alone",))
    text = stdin() if list(texts) == [STDIN] else "\n".join(texts)
    return text if text.replace(",", " ").strip() else Failed(ExitCode.USAGE, ("nothing to add",))


def _add(ctx: Context, args: argparse.Namespace) -> Outcome:
    text = input_text(args.text, ctx.stdin)
    if isinstance(text, Failed):
        return text
    return add_outcome(add_pasted(ctx.conn, BatchKind.CLI, ctx.actor, text, AddOptions(ReconnectMode.AUTO), ctx.now))


def register(commands: Commands) -> None:
    add = commands.command("add", "add domains from text as the web paste does: URLs and names, duplicates skipped, no questions", _add)
    add.add_argument("text", nargs="+", metavar="TEXT_OR_URL", help=f"text to parse, each argument a line; '{STDIN}' reads it from stdin")
