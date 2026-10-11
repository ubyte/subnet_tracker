import http.client
import threading
from collections.abc import Iterator
from ipaddress import IPv4Address, IPv6Address

from flask import Flask, Response, request
from waitress.server import BaseWSGIServer, create_server

from subnet_tracker.config import Endpoint
from subnet_tracker.web.server import listen_spec
from subnet_tracker.web.streaming import streamed
from web_world import GZIP, gunzip_stream


def test_listen_spec_lists_every_bind_address() -> None:
    assert listen_spec((Endpoint(IPv4Address("10.8.0.1"), 8080), Endpoint(IPv6Address("::1"), 8081))) == "10.8.0.1:8080 [::1]:8081"


def streaming_app(release: threading.Event) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def page() -> Response:
        def blocks() -> Iterator[str]:
            yield "<form>"
            assert release.wait(10)
            yield "<table>"

        return streamed(blocks(), request.headers["Accept-Encoding"])

    return app


def test_waitress_delivers_a_block_before_the_page_is_complete() -> None:
    release = threading.Event()
    server = create_server(streaming_app(release), listen=listen_spec((Endpoint(IPv4Address("127.0.0.1"), 0),)))
    assert isinstance(server, BaseWSGIServer)
    threading.Thread(target=server.run, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(server.getsockname()[1]), timeout=10)
        conn.request("GET", "/", headers=GZIP)
        response = conn.getresponse()
        decompressor, received = gunzip_stream(), b""
        while received != b"<form>":
            chunk = response.read1()
            assert chunk
            received += decompressor.decompress(chunk)
        release.set()
        assert decompressor.decompress(response.read()) + decompressor.flush() == b"<table>"
        conn.close()
    finally:
        release.set()
        server.close()
