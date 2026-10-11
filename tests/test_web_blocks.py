from collections.abc import Iterator

import pytest

from subnet_tracker.web.blocks import FLUSH, split_blocks


@pytest.mark.parametrize(
    ("pieces", "blocks"),
    [
        ([], []),
        (["<html>", "<head>", "</head>"], ["<html><head></head>"]),
        (["<head></head>", FLUSH, "<body>", "</body>"], ["<head></head>", "<body></body>"]),
        ([f"</head>{FLUSH}<body>", "x", f"</form>{FLUSH}"], ["</head>", "<body>x</form>"]),
        ([FLUSH, "a", FLUSH, FLUSH, f"{FLUSH}b{FLUSH}c"], ["a", "b", "c"]),
    ],
)
def test_split_blocks(pieces: list[str], blocks: list[str]) -> None:
    assert list(split_blocks(pieces)) == blocks


def test_a_block_is_yielded_before_the_next_pieces_are_read() -> None:
    def pieces() -> Iterator[str]:
        yield "<head>"
        yield f"</head>{FLUSH}<body>"
        raise AssertionError("pieces after the first block were read too early")

    assert next(split_blocks(pieces())) == "<head></head>"
