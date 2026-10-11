import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from hypothesis import settings

from cli_world import Cli, make_cli
from db_rows import seed
from subnet_tracker.core.db import connect, migrate

settings.register_profile("default", deadline=None)
settings.register_profile("ci", deadline=None, max_examples=500)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(tmp_path / "state.db")) as conn:
        migrate(conn)
        yield conn


@pytest.fixture
def seeded_db(db: sqlite3.Connection) -> sqlite3.Connection:
    seed(db)
    return db


@pytest.fixture
def cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Cli:
    return make_cli(tmp_path, capsys)


@pytest.fixture
def other(db: sqlite3.Connection, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(tmp_path / "state.db")) as conn:
        yield conn
