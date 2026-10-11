import re
import zlib
from collections.abc import Iterable, Iterator

# 16 + MAX_WBITS selects the gzip wrapper rather than a raw zlib stream.
_GZIP_WBITS = 16 + zlib.MAX_WBITS
_QVALUE = re.compile(r"^(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)$")


def _coding(part: str) -> tuple[str, float] | None:
    name, separator, params = part.strip().partition(";")
    name = name.strip().lower()
    if not name:
        return None
    if not separator:
        return name, 1.0
    key, equals, raw = params.strip().partition("=")
    if key.strip().lower() != "q" or not equals or not _QVALUE.fullmatch(raw.strip()):
        return None
    return name, float(raw.strip())


def accepts_gzip(header: str) -> bool:
    # An explicit gzip or x-gzip q=0 refuses compression even when "*" accepts it.
    gzip_q: float | None = None
    star_q: float | None = None
    for part in header.split(","):
        parsed = _coding(part)
        if parsed is None:
            continue
        name, q = parsed
        if name == "gzip" or (name == "x-gzip" and gzip_q is None):
            gzip_q = q
        elif name == "*" and star_q is None:
            star_q = q
    if gzip_q is not None:
        return gzip_q > 0
    return star_q is not None and star_q > 0


def gzip_blocks(blocks: Iterable[bytes]) -> Iterator[bytes]:
    # Z_SYNC_FLUSH ends each block so it can be decoded before the next block exists. The last chunk is the gzip trailer.
    compressor = zlib.compressobj(wbits=_GZIP_WBITS)
    for block in blocks:
        if not block:
            continue
        yield compressor.compress(block) + compressor.flush(zlib.Z_SYNC_FLUSH)
    yield compressor.flush()
