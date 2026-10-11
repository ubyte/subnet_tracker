import gzip
import hashlib
import re
from importlib.resources import files
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from subnet_tracker.config import Config
from subnet_tracker.web.app import create_app
from subnet_tracker.web.assets import build_assets, hashed_name, load_assets
from web_world import GZIP, packaged_static

IMMUTABLE = "max-age=31536000, immutable"


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    return create_app(Config(db=tmp_path / "state.db"))


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def pico_href(client: FlaskClient) -> str:
    m = re.search(r'<link rel="stylesheet" href="([^"]+)">', client.get("/").text)
    assert m
    return m[1]


def test_hashed_name_puts_the_content_hash_before_the_extension() -> None:
    digest = hashlib.sha256(b"main{}").hexdigest()[:12]
    assert hashed_name("pico.min.css", b"main{}") == f"pico.min.{digest}.css"
    assert hashed_name("LICENSE", b"main{}") == f"LICENSE.{digest}"
    assert hashed_name("app.css", b"main{ }") != hashed_name("app.css", b"main{}")


def test_build_assets_indexes_by_both_names_and_pre_gzips_text() -> None:
    assets = build_assets({"app.css": b"main{}", "page.js": b"// @ts-check", "logo.png": b"\x89PNG"})
    css = assets.by_name["app.css"]
    assert (css.url_name, css.mimetype, css.data) == (hashed_name("app.css", b"main{}"), "text/css", b"main{}")
    assert assets.by_url_name[css.url_name] == css
    assert css.gzipped is not None
    assert gzip.decompress(css.gzipped) == b"main{}"
    js = assets.by_name["page.js"].gzipped
    assert js is not None
    assert gzip.decompress(js) == b"// @ts-check"
    assert assets.by_name["logo.png"].gzipped is None


def test_the_page_links_pico_by_its_content_hash(client: FlaskClient) -> None:
    assert pico_href(client) == f"/static/{hashed_name('pico.min.css', packaged_static('pico.min.css'))}"


def test_a_static_file_is_served_immutable(client: FlaskClient) -> None:
    response = client.get(pico_href(client))
    assert response.status_code == 200
    assert response.data == packaged_static("pico.min.css")
    assert (response.mimetype, response.headers["Cache-Control"], response.headers["Vary"]) == ("text/css", IMMUTABLE, "Accept-Encoding")
    assert "Content-Encoding" not in response.headers


def test_a_static_file_is_gzipped_when_the_client_accepts_it(client: FlaskClient) -> None:
    response = client.get(pico_href(client), headers=GZIP)
    assert (response.headers["Content-Encoding"], response.headers["Cache-Control"]) == ("gzip", IMMUTABLE)
    assert gzip.decompress(response.data) == packaged_static("pico.min.css")


@pytest.mark.parametrize("path", ["/static/pico.min.css", "/static/pico.min.000000000000.css", "/static/missing.css", "/static/../app.py"])
def test_names_without_the_current_hash_are_not_found(client: FlaskClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_every_packaged_static_file_is_served(client: FlaskClient) -> None:
    assets = load_assets(files("subnet_tracker.web").joinpath("static"))
    assert "pico.min.css" in assets.by_name
    for asset in assets.by_name.values():
        assert client.get(f"/static/{asset.url_name}").data == asset.data
