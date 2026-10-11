import argparse
import os
import pwd
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from subnet_tracker.cli.output import Outcome
from subnet_tracker.config import Config
from subnet_tracker.core.trigger import TRIGGER_FILE
from subnet_tracker.tick import TICK_LOCK
from subnet_tracker.types import Timestamp


def _clock() -> Timestamp:
    return Timestamp(int(time.time()))


def _sleep(seconds: int) -> None:
    time.sleep(seconds)


def unix_user(uid: int, environ: Mapping[str, str]) -> str:
    if uid == 0 and (sudo_user := environ.get("SUDO_USER")):
        return sudo_user
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _process_user() -> str:
    return unix_user(os.getuid(), os.environ)


def _read_stdin() -> str:
    return sys.stdin.read()


def _ask_on_terminal(prompt: str) -> bool | None:
    if not sys.stdin.isatty():
        return None
    sys.stderr.write(f"{prompt} [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


type Confirm = Callable[[str], bool | None]


@dataclass(frozen=True, slots=True)
class Runtime:
    trigger_file: Path = TRIGGER_FILE
    clock: Callable[[], Timestamp] = _clock
    user: Callable[[], str] = _process_user
    stdin: Callable[[], str] = _read_stdin
    # None when there is no terminal to ask on
    confirm: Confirm = _ask_on_terminal
    sleep: Callable[[int], None] = _sleep
    lock_file: Path = TICK_LOCK


@dataclass(frozen=True, slots=True)
class Context:
    config: Config
    conn: sqlite3.Connection
    now: Timestamp
    actor: str
    stdin: Callable[[], str]
    confirm: Confirm
    clock: Callable[[], Timestamp]
    sleep: Callable[[int], None]
    lock_file: Path


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
