from collections.abc import Sequence
from ipaddress import IPv4Address

import pytest
from flask import Flask

from cli_world import Cli
from subnet_tracker.config import Endpoint
from subnet_tracker.web import server


@pytest.fixture
def served(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Flask, Sequence[Endpoint]]]:
    calls: list[tuple[Flask, Sequence[Endpoint]]] = []
    monkeypatch.setattr(server, "serve", lambda app, bind: calls.append((app, bind)))
    cli.config.write_text(cli.config.read_text(encoding="utf-8") + '[web]\nbind = ["127.0.0.1:18080"]\n', encoding="utf-8")
    return calls


def test_web_serves_the_app_on_the_configured_addresses(cli: Cli, served: list[tuple[Flask, Sequence[Endpoint]]]) -> None:
    assert cli("db", "migrate").code == 0
    assert (cli("web").code, served[0][1]) == (0, (Endpoint(IPv4Address("127.0.0.1"), 18080),))
    assert served[0][0].test_client().get("/").status_code == 200


def test_web_needs_a_migrated_database(cli: Cli, served: list[tuple[Flask, Sequence[Endpoint]]]) -> None:
    result = cli("web")
    assert (result.code, served) == (1, [])
    assert "db migrate" in result.err


def test_an_address_that_cannot_be_bound_exits_1(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object) -> None:
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(server, "serve", refuse)
    assert cli("db", "migrate").code == 0
    result = cli("web")
    assert result.code == 1
    assert "Address already in use" in result.err
