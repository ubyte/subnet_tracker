import argparse

from subnet_tracker.cli.add_summary import counted, invalid_json, invalid_text
from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, ExitCode, Failed, Outcome, listed, moment, table
from subnet_tracker.core.batches import Batch, BatchSummary, get_batch, list_batches
from subnet_tracker.core.services.batches import undo_add
from subnet_tracker.types import BatchId, JsonValue

DEFAULT_LIMIT = 20
LIST_HEADERS = ("id", "created", "kind", "actor", "added", "duplicates", "invalid", "undone")


def positive(text: str) -> int:
    if (value := int(text)) < 1:
        raise ValueError(text)
    return value


def batch_json(b: Batch) -> dict[str, JsonValue]:
    return {"id": b.id, "created_at": b.created_at, "kind": b.kind, "actor": b.actor, "added": b.added, "duplicates": b.duplicates, "invalid": invalid_json(b.invalid), "undone_at": b.undone_at}


def batch_text(summary: BatchSummary) -> str:
    b = summary.batch
    head = f"batch {b.id}: {b.kind} by {b.actor} at {moment(b.created_at)}" + ("" if b.undone_at is None else f", undone {moment(b.undone_at)}")
    counts = f"added {b.added}, skipped {b.duplicates}, not recognized {len(b.invalid)}"
    return "\n".join([head, counts, *invalid_text(b.invalid), f"domains: {listed([d.canonical.name for d in summary.domains])}"])


def _unknown(batch_id: int) -> Failed:
    return Failed(ExitCode.USAGE, (f"batch {batch_id} does not exist",))


def _list(ctx: Context, args: argparse.Namespace) -> Outcome:
    batches = list_batches(ctx.conn, args.limit)
    rows = [(str(b.id), moment(b.created_at), b.kind, b.actor, str(b.added), str(b.duplicates), str(len(b.invalid)), moment(b.undone_at)) for b in batches]
    return Done(table(LIST_HEADERS, rows), [batch_json(b) for b in batches])


def _show(ctx: Context, args: argparse.Namespace) -> Outcome:
    if (summary := get_batch(ctx.conn, BatchId(args.id))) is None:
        return _unknown(args.id)
    return Done(batch_text(summary), batch_json(summary.batch) | {"input": summary.batch.input, "domains": [d.canonical.name for d in summary.domains]})


def _undo(ctx: Context, args: argparse.Namespace) -> Outcome:
    if (removed := undo_add(ctx.conn, BatchId(args.id), ctx.actor, ctx.now)) is None:
        return _unknown(args.id)
    names = [d.canonical.name for d in removed]
    return Done(counted(f"batch {args.id} undone, deleted {len(names)}", names), {"batch_id": args.id, "deleted": names}, notify=bool(removed))


def register(commands: Commands) -> None:
    batch = commands.group("batch", "additions: a paste, scan, import or CLI add each make a batch")
    batch.command("list", "list batches, newest first", _list).add_argument("--limit", type=positive, default=DEFAULT_LIMIT, metavar="N", help=f"{DEFAULT_LIMIT} by default")
    batch.command("show", "show a batch with its unrecognized lines and the domains that remain", _show).add_argument("id", type=int, metavar="ID")
    batch.command("undo", "delete the domains of a batch that still exist", _undo).add_argument("id", type=int, metavar="ID")
