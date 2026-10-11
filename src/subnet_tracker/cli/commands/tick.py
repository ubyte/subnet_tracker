import argparse

from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, Outcome
from subnet_tracker.tick import IdleApply, IdleResolve, TickBusy, TickReport, run_tick
from subnet_tracker.types import JsonValue


def tick_text(report: TickReport) -> str:
    return f"finished {report.requests_finished} requests, {report.portions} portions"


def tick_json(report: TickReport) -> JsonValue:
    return {
        "requests_finished": report.requests_finished,
        "portions": report.portions,
        "reconnected": report.reconnected,
        "waited_seconds": report.waited_seconds,
        "resolve_runs_pruned": report.resolve_runs_pruned,
        "apply_runs_pruned": report.apply_runs_pruned,
        "events_pruned": report.events_pruned,
    }


def _tick(ctx: Context, args: argparse.Namespace) -> Outcome:
    try:
        report = run_tick(ctx.conn, resolve=IdleResolve(), apply=IdleApply(), clock=ctx.clock, sleep=ctx.sleep, lock=ctx.lock_file)
    except TickBusy:
        return Done("tick already running", {"busy": True})
    # Tick queues no request, so the trigger file is left untouched; truncating it would start another tick.
    return Done(tick_text(report), tick_json(report))


def register(commands: Commands) -> None:
    commands.command("tick", "resolve, apply, and reconnect whatever is due", _tick)
