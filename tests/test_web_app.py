import gzip
import sqlite3
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from subnet_tracker.config import Config
from subnet_tracker.web.app import create_app
from subnet_tracker.web.blocks import FLUSH
from subnet_tracker.web.context import app_config, request_db
from web_world import GZIP, gunzip_stream


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    return create_app(Config(db=tmp_path / "state.db"))


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def test_the_main_page_is_streamed_html_from_the_base_template(client: FlaskClient) -> None:
    response = client.get("/")
    assert (response.status_code, response.mimetype, response.charset) == (200, "text/html", "utf-8")
    assert response.is_streamed
    assert response.text.startswith("<!doctype html>")
    assert "<title>subnet-tracker</title>" in response.text
    assert FLUSH not in response.text
    assert response.headers["Vary"] == "Accept-Encoding"
    assert "Content-Encoding" not in response.headers


def test_the_main_page_is_gzipped_when_the_client_accepts_it(client: FlaskClient) -> None:
    response = client.get("/", headers=GZIP)
    assert (response.headers["Content-Encoding"], response.headers["Vary"]) == ("gzip", "Accept-Encoding")
    assert gzip.decompress(response.data) == client.get("/").data


def test_the_head_is_sent_as_a_block_of_its_own(client: FlaskClient) -> None:
    response = client.get("/", headers=GZIP, buffered=False)
    first = gunzip_stream().decompress(next(iter(response.response))).decode()
    response.close()
    assert first.startswith("<!doctype html>")
    assert first.rstrip().endswith("</head>")


def test_an_unknown_path_is_not_found(client: FlaskClient) -> None:
    assert client.get("/nowhere").status_code == 404


def test_a_request_opens_the_database_once_on_demand_and_closes_it(app: Flask, tmp_path: Path) -> None:
    opened: list[sqlite3.Connection] = []

    @app.get("/probe")
    def probe() -> str:
        opened.extend((request_db(), request_db()))
        return str(app_config().db)

    client = app.test_client()
    client.get("/")
    assert not (tmp_path / "state.db").exists()
    assert client.get("/probe").text == str(tmp_path / "state.db")
    client.get("/probe")
    first, again, second, _ = opened
    assert first is again
    assert first is not second
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")
