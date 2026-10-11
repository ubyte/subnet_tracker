import io
import os
import pwd
import sqlite3
import subprocess
import sys
from contextlib import closing
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from cli_world import Cli, Result
from subnet_tracker.cli.command import Runtime, unix_user
from subnet_tracker.cli.main import main
from subnet_tracker.config import CONFIG_FILE
from subnet_tracker.core.db import connect, load_migrations
from subnet_tracker.core.resolvers import DEFAULT_RESOLVERS, list_resolvers
from subnet_tracker.core.trigger import TRIGGER_FILE

LATEST = load_migrations()[-1].version


def test_no_command_is_a_usage_error(cli: Cli) -> None:
    result = cli()
    assert (result.code, result.out) == (2, "")
    assert "usage: subnet-tracker" in result.err


@pytest.mark.parametrize("argv", [("frobnicate",), ("settings",), ("settings", "get"), ("settings", "set", "resolve.interval"), ("db", "migrate", "extra")])
def test_bad_arguments_are_usage_errors(cli: Cli, argv: tuple[str, ...]) -> None:
    assert cli(*argv).code == 2


def test_help_lists_the_commands(cli: Cli) -> None:
    result = cli("--help")
    assert (result.code, result.err) == (0, "")
    assert all(name in result.out for name in ("settings", "db", "add", "domain", "batch", "tick"))


def test_defaults_are_the_installed_paths_and_the_unix_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    runtime = Runtime()
    assert (CONFIG_FILE, runtime.trigger_file, runtime.user()) == (Path("/etc/subnet-tracker/config.toml"), TRIGGER_FILE, pwd.getpwuid(os.getuid()).pw_name)


ROOT = pwd.getpwuid(0).pw_name


@pytest.mark.parametrize(("environ", "user"), [({"SUDO_USER": "alice"}, "alice"), ({}, ROOT), ({"SUDO_USER": ""}, ROOT)])
def test_sudo_caller_is_the_user_for_root(environ: dict[str, str], user: str) -> None:
    assert unix_user(0, environ) == user


def test_sudo_caller_is_ignored_for_other_uids() -> None:
    someone = next((p for p in pwd.getpwall() if p.pw_uid != 0), None)
    if someone is None:
        pytest.skip("no passwd entry besides root")
    assert unix_user(someone.pw_uid, {"SUDO_USER": "alice"}) == someone.pw_name


def test_default_stdin_is_the_process_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("a.com\n"))
    assert Runtime().stdin() == "a.com\n"


def test_default_confirmation_needs_a_terminal(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    assert Runtime().confirm("delete a.com?") is None
    assert capsys.readouterr().err == ""


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(("answer", "confirmed"), [("y\n", True), (" YES \n", True), ("\n", False), ("no\n", False), ("", False)])
def test_default_confirmation_asks_on_stderr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], answer: str, confirmed: bool) -> None:
    monkeypatch.setattr(sys, "stdin", Terminal(answer))
    assert Runtime().confirm("delete a.com?") is confirmed
    assert capsys.readouterr().err == "delete a.com? [y/N] "


def test_uid_without_passwd_entry_is_the_number(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(uid: int) -> pwd.struct_passwd:
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", missing)
    assert unix_user(4242, {}) == "4242"


def test_missing_config_is_an_error(cli: Cli, tmp_path: Path) -> None:
    cli.config.unlink()
    result = cli("settings", "list")
    assert (result.code, result.out) == (1, "")
    assert result.err.startswith(f"subnet-tracker: error: {tmp_path / 'config.toml'}: ")


def test_invalid_config_is_an_error(cli: Cli) -> None:
    cli.config.write_text("db = 1\n")
    assert cli("settings", "list") == Result(1, "", f"subnet-tracker: error: {cli.config}: db: expected a string\n")


def test_missing_database_is_not_created(cli: Cli, tmp_path: Path) -> None:
    assert cli("settings", "list") == Result(1, "", f"subnet-tracker: error: database {tmp_path / 'state.db'} does not exist, create it with 'subnet-tracker db migrate'\n")
    assert not (tmp_path / "state.db").exists()


def test_outdated_schema_is_an_error(cli: Cli, tmp_path: Path) -> None:
    connect(tmp_path / "state.db").close()
    assert cli("settings", "list") == Result(1, "", f"subnet-tracker: error: database {tmp_path / 'state.db'} has schema version 0, {LATEST} is needed: run 'subnet-tracker db migrate'\n")


def test_newer_schema_is_an_error(cli: Cli, db: sqlite3.Connection) -> None:
    db.execute(f"PRAGMA user_version = {LATEST + 1}")
    result = cli("settings", "list")
    assert result.code == 1
    assert "newer than the latest known migration" in result.err


def test_migrate_creates_the_database_with_default_resolvers(cli: Cli, tmp_path: Path) -> None:
    assert cli("db", "migrate") == Result(0, f"applied migrations {', '.join(map(str, range(1, LATEST + 1)))}, schema version {LATEST}\n", "")
    with closing(connect(tmp_path / "state.db")) as conn:
        assert [r.name for r in list_resolvers(conn)] == [r.name for r in DEFAULT_RESOLVERS]
    assert not (tmp_path / "trigger").exists()


def test_migrate_again_is_up_to_date(cli: Cli) -> None:
    cli("db", "migrate")
    assert cli("db", "migrate") == Result(0, f"schema version {LATEST} is up to date\n", "")


def test_migrate_json(cli: Cli) -> None:
    assert cli.json("db", "migrate") == {"applied": list(range(1, LATEST + 1)), "version": LATEST}
    assert cli.json("db", "migrate") == {"applied": [], "version": LATEST}


def test_database_failure_is_an_error(cli: Cli, tmp_path: Path) -> None:
    cli.config.write_text(f'db = "{tmp_path / "absent" / "state.db"}"\n')
    result = cli("db", "migrate")
    assert (result.code, result.out) == (1, "")
    assert result.err.startswith("subnet-tracker: error: ")


def test_main_reads_sys_argv(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["subnet-tracker", "--help"])
    assert main() == 0
    assert "usage: subnet-tracker" in capsys.readouterr().out


def test_entry_point_is_main() -> None:
    (script,) = entry_points(group="console_scripts", name="subnet-tracker")
    assert script.load() is main


def test_package_runs_as_a_module() -> None:
    done = subprocess.run([sys.executable, "-m", "subnet_tracker", "--help"], capture_output=True, text=True, timeout=60, check=False)
    assert (done.returncode, done.stderr) == (0, "")
    assert "usage: subnet-tracker" in done.stdout
