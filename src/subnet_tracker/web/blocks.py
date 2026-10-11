from collections.abc import Iterable, Iterator

# Templates mark the end of a block with {{ flush }}. The marker is an HTML comment and is removed from the response.
FLUSH = "<!--flush-->"


def split_blocks(pieces: Iterable[str]) -> Iterator[str]:
    pending: list[str] = []
    for piece in pieces:
        head, *rest = piece.split(FLUSH)
        pending.append(head)
        for part in rest:
            if block := "".join(pending):
                yield block
            pending = [part]
    if block := "".join(pending):
        yield block
