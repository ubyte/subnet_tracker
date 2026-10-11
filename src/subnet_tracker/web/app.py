from importlib.resources import files
from pathlib import Path

from flask import Flask, Response, abort, request
from markupsafe import Markup

from subnet_tracker.config import Config
from subnet_tracker.web.assets import Asset, asset_url, load_assets
from subnet_tracker.web.blocks import FLUSH
from subnet_tracker.web.context import close_request_db, install_config
from subnet_tracker.web.gzip_stream import accepts_gzip
from subnet_tracker.web.streaming import stream_template

_IMMUTABLE = "max-age=31536000, immutable"
_ROOT = str(Path(__file__).resolve().parent)


def _asset_response(asset: Asset, accept_encoding: str) -> Response:
    headers = {"Cache-Control": _IMMUTABLE, "Vary": "Accept-Encoding"}
    body = asset.data
    if asset.gzipped is not None and accepts_gzip(accept_encoding):
        body = asset.gzipped
        headers["Content-Encoding"] = "gzip"
    return Response(body, mimetype=asset.mimetype, headers=headers)


def create_app(config: Config) -> Flask:
    app = Flask("subnet_tracker.web", static_folder=None, root_path=_ROOT)
    install_config(app, config)
    assets = load_assets(files("subnet_tracker.web").joinpath("static"))

    def static_url(name: str) -> str:
        return asset_url(assets, name)

    @app.context_processor
    def template_context() -> dict[str, object]:
        return {"static_url": static_url, "flush": Markup(FLUSH)}

    @app.get("/")
    def index() -> Response:
        return stream_template("base.html", request.headers.get("Accept-Encoding", ""))

    @app.get("/static/<path:name>")
    def static_file(name: str) -> Response:
        asset = assets.by_url_name.get(name)
        if asset is None:
            abort(404)
        return _asset_response(asset, request.headers.get("Accept-Encoding", ""))

    app.teardown_request(close_request_db)
    return app
