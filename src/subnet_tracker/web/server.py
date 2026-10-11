from collections.abc import Sequence

from flask import Flask
from waitress import serve as waitress_serve

from subnet_tracker.config import Endpoint


def listen_spec(bind: Sequence[Endpoint]) -> str:
    return " ".join(map(str, bind))


def serve(app: Flask, bind: Sequence[Endpoint]) -> None:
    # Waitress defaults: X-Forwarded-* is ignored, and a flushed block is sent before the next one is pulled.
    waitress_serve(app, listen=listen_spec(bind))
