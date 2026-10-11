import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from subnet_tracker.cli.command import Runtime
from subnet_tracker.cli.main import main
from subnet_tracker.types import JsonValue, Timestamp

NOW = Timestamp(1_760_000_000)
USER = "alice"
ACTOR = f"cli:{USER}"


@dataclass(frozen=True, slots=True)
class Result:
    code: int
    out: str
    err: str


@dataclass(frozen=True, slots=True)
class Cli:
    config: Path
    runtime: Runtime
    capsys: pytest.CaptureFixture[str]

    def __call__(self, *argv: str) -> Result:
        code = main(["--config", str(self.config), *argv], self.runtime)
        captured = self.capsys.readouterr()
        return Result(code, captured.out, captured.err)

    def json(self, *argv: str) -> JsonValue:
        result = self(*argv, "--json")
        assert (result.code, result.err) == (0, "")
        data: JsonValue = json.loads(result.out)
        return data


def write_config(path: Path, db: Path) -> Path:
    path.write_text(f"db = {json.dumps(str(db))}\n", encoding="utf-8")
    return path


def no_stdin() -> str:
    raise AssertionError("the command reads stdin")


def no_terminal(prompt: str) -> bool | None:
    return None


def make_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Cli:
    return Cli(write_config(tmp_path / "config.toml", tmp_path / "state.db"), Runtime(tmp_path / "trigger", lambda: NOW, lambda: USER, no_stdin, no_terminal), capsys)


def reading(cli: Cli, text: str) -> Cli:
    return replace(cli, runtime=replace(cli.runtime, stdin=lambda: text))


def answering(cli: Cli, answer: bool | None, prompts: list[str]) -> Cli:
    def confirm(prompt: str) -> bool | None:
        prompts.append(prompt)
        return answer

    return replace(cli, runtime=replace(cli.runtime, confirm=confirm))


def trigger_lines(tmp_path: Path) -> list[str]:
    trigger = tmp_path / "trigger"
    return trigger.read_text().splitlines() if trigger.exists() else []
