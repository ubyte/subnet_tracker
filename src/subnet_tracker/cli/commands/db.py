import argparse
from collections.abc import Sequence

from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, Outcome
from subnet_tracker.core.db import schema_version
from subnet_tracker.core.install import migrate_database


def migrate_text(applied: Sequence[int], version: int) -> str:
    if not applied:
        return f"schema version {version} is up to date"
    return f"applied migrations {', '.join(map(str, applied))}, schema version {version}"


def _migrate(ctx: Context, args: argparse.Namespace) -> Outcome:
    applied = migrate_database(ctx.conn, ctx.now)
    version = schema_version(ctx.conn)
    return Done(migrate_text(applied, version), {"applied": list(applied), "version": version})


def register(commands: Commands) -> None:
    commands.group("db", "database maintenance").command("migrate", "create the database or bring its schema up to date", _migrate, requires_schema=False)
