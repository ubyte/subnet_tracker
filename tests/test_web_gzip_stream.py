import gzip
from collections.abc import Iterator

import pytest
from hypothesis import given
from hypothesis import strategies as st

from subnet_tracker.web.gzip_stream import accepts_gzip, gzip_blocks
from web_world import gunzip_stream


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("", False),
        ("gzip", True),
        ("GZip", True),
        ("x-gzip", True),
        ("deflate, gzip;q=0.5", True),
        ("br, deflate", False),
        ("identity", False),
        ("gzip;q=0", False),
        ("gzip; q=0.000", False),
        ("gzip;q=0.001", True),
        ("gzip;q=oops", False),
        ("*", True),
        ("*;q=0", False),
        ("*, gzip;q=0", False),
        ("gzip;q=0, *", False),
        ("br;q=1, *;q=0.1", True),
    ],
)
def test_accepts_gzip(header: str, expected: bool) -> None:
    assert accepts_gzip(header) is expected


@given(st.lists(st.binary(max_size=300), max_size=8))
def test_each_chunk_completes_every_block_before_it(blocks: list[bytes]) -> None:
    chunks = list(gzip_blocks(blocks))
    nonempty = [b for b in blocks if b]
    decompressor, seen = gunzip_stream(), b""
    for i, chunk in enumerate(chunks[:-1]):
        seen += decompressor.decompress(chunk)
        assert seen == b"".join(nonempty[: i + 1])
    assert len(chunks) == len(nonempty) + 1
    assert gzip.decompress(b"".join(chunks)) == b"".join(blocks)


def test_a_block_is_sent_before_the_next_one_is_produced() -> None:
    def blocks() -> Iterator[bytes]:
        yield b"<head></head>"
        raise AssertionError("the next block was requested before the first one was sent")

    assert gunzip_stream().decompress(next(gzip_blocks(blocks()))) == b"<head></head>"
