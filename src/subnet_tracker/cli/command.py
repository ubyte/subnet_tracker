import argparse
import os
import pwd
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from subnet_tracker.cli.output import Outcome
from subnet_tracker.config import Config
from subnet_tracker.core.trigger import TRIGGER_FILE
from subnet_tracker.types import Timestamp


def _clock() -> Timestamp:
    return Timestamp(int(time.time()))


def _unix_user() -> str:
    uid = os.getuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


@dataclass(frozen=True, slots=True)
class Runtime:
    trigger_file: Path = TRIGGER_FILE
    clock: Callable[[], Timestamp] = _clock
    user: Callable[[], str] = _unix_user


@dataclass(frozen=True, slots=True)
class Context:
    config: Config
    conn: sqlite3.Connection
    now: Timestamp
    actor: str


type Handler = Callable[[Context, argparse.Namespace], Outcome]


def cli_actor(user: str) -> str:
    return f"cli:{user}"


@dataclass(frozen=True, slots=True)
class Commands:
    parsers: "argparse._SubParsersAction[argparse.ArgumentParser]"

    def group(self, name: str, help: str) -> "Commands":
        return Commands(self.parsers.add_parser(name, help=help, description=help).add_subparsers(required=True, metavar="COMMAND"))

    # requires_schema=False only for the command that creates or upgrades the database
    def command(self, name: str, help: str, run: Handler, *, requires_schema: bool = True) -> argparse.ArgumentParser:
        parser = self.parsers.add_parser(name, help=help, description=help)
        parser.add_argument("--json", action="store_true", help="print the result as JSON")
        parser.set_defaults(run=run, requires_schema=requires_schema)
        return parser
