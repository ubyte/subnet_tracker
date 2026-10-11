import argparse

from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, Outcome
from subnet_tracker.web import server
from subnet_tracker.web.app import create_app


def _serve(ctx: Context, args: argparse.Namespace) -> Outcome:
    bind = ctx.config.web.bind
    server.serve(create_app(ctx.config), bind)
    return Done("", [str(endpoint) for endpoint in bind])


def register(commands: Commands) -> None:
    commands.command("web", "serve the web UI", _serve)
