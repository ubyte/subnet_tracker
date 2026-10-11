from collections.abc import Iterable, Iterator

from flask import Response
from flask import stream_template as render_stream

from subnet_tracker.web.blocks import split_blocks
from subnet_tracker.web.gzip_stream import accepts_gzip, gzip_blocks


def _encode(blocks: Iterable[str]) -> Iterator[bytes]:
    return (block.encode() for block in blocks)


def streamed(blocks: Iterable[str], accept_encoding: str) -> Response:
    if accepts_gzip(accept_encoding):
        return Response(gzip_blocks(_encode(blocks)), mimetype="text/html", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(_encode(blocks), mimetype="text/html", headers={"Vary": "Accept-Encoding"})


def stream_template(name: str, accept_encoding: str) -> Response:
    return streamed(split_blocks(render_stream(name)), accept_encoding)
