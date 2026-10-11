import zlib
from importlib.resources import files

GZIP = {"Accept-Encoding": "gzip"}


def packaged_static(name: str) -> bytes:
    return files("subnet_tracker.web").joinpath("static", name).read_bytes()


def gunzip_stream() -> "zlib._Decompress":
    return zlib.decompressobj(16 + zlib.MAX_WBITS)
