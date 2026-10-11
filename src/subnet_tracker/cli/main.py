import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path

from subnet_tracker.cli.command import Commands, Context, Runtime, cli_actor
from subnet_tracker.cli.commands import add, batch, db, domain, group, resolver, route, settings, tag, tick, transfer, web
from subnet_tracker.cli.output import PROG, Done, ExitCode, Failed, Outcome, render, warning
from subnet_tracker.config import CONFIG_FILE, Config, ConfigError, load_config
from subnet_tracker.core.db import MigrationError, connect, load_migrations, pending_migrations, schema_version
from subnet_tracker.core.settings import SettingsError
from subnet_tracker.core.trigger import notify_tick

COMMANDS: tuple[Callable[[Commands], None], ...] = (
    add.register,
    domain.register,
    group.register,
    tag.register,
    route.register,
    resolver.register,
    batch.register,
    settings.register,
    transfer.register,
    db.register,
    tick.register,
    web.register,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Domain-based split tunnel for OpenVPN: tracks domain addresses and pushes routes to their networks")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, metavar="PATH", help=f"configuration file, {CONFIG_FILE} by default")
    commands = Commands(parser.add_subparsers(required=True, metavar="COMMAND"))
    for register in COMMANDS:
        register(commands)
    return parser


def schema_error(path: Path, version: int) -> str | None:
    migrations = load_migrations()
    if not pending_migrations(migrations, version):
        return None
    return f"database {path} has schema version {version}, {migrations[-1].version} is needed: run '{PROG} db migrate'"


def _execute(config: Config, args: argparse.Namespace, runtime: Runtime) -> tuple[Outcome, OSError | None]:
    if args.requires_schema and not config.db.exists():
        return Failed(ExitCode.ERROR, (f"database {config.db} does not exist, create it with '{PROG} db migrate'",)), None
    with closing(connect(config.db)) as conn:
        if args.requires_schema and (error := schema_error(config.db, schema_version(conn))):
            return Failed(ExitCode.ERROR, (error,)), None
        now, actor = runtime.clock(), cli_actor(runtime.user())
        outcome: Outcome = args.run(Context(config, conn, now, actor, runtime.stdin, runtime.confirm, runtime.clock, runtime.sleep, runtime.lock_file, runtime.write_output), args)
        notified = notify_tick(conn, now, actor, runtime.trigger_file) if isinstance(outcome, Done) and outcome.notify else None
    return outcome, notified


def _run(args: argparse.Namespace, runtime: Runtime) -> tuple[Outcome, OSError | None]:
    config = load_config(args.config)
    try:
        return _execute(config, args, runtime)
    except sqlite3.Error as e:
        return Failed(ExitCode.ERROR, (f"database {config.db}: {e}",)), None


def main(argv: Sequence[str] | None = None, runtime: Runtime = Runtime()) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else ExitCode.USAGE
    try:
        outcome, trigger_error = _run(args, runtime)
    except (ConfigError, MigrationError, SettingsError, OSError) as e:
        outcome, trigger_error = Failed(ExitCode.ERROR, (str(e),)), None
    rendered = render(outcome, args.json)
    sys.stdout.write(rendered.stdout)
    sys.stderr.write(rendered.stderr)
    if trigger_error is not None:
        sys.stderr.write(warning(f"tick not notified: {trigger_error}; the next scheduled tick picks the change up"))
    return outcome.code
