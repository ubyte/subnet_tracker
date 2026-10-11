import re
from dataclasses import dataclass

from subnet_tracker.discovery.parse import Rejected

QUOTED = r'"((?:[^"\\]|\\.)*)"'
SIGILS = "#@"
_BARE = r'[^\s";]+'
_WORD = re.compile(rf"(?:([{SIGILS}]){QUOTED}|({_BARE}))(?=[\s;]|$)")
_BARE_WORD = re.compile(_BARE)
_ESCAPE = re.compile(r"\\(.)")
_NEEDS_ESCAPE = re.compile(r'(["\\])')


@dataclass(frozen=True, slots=True)
class Word:
    sigil: str
    text: str
    raw: str


@dataclass(frozen=True, slots=True)
class Tokens:
    words: tuple[Word, ...]
    comment: str


def unescape(quoted: str) -> str:
    return _ESCAPE.sub(r"\1", quoted)


def quoted(text: str) -> str:
    return '"' + _NEEDS_ESCAPE.sub(r"\\\1", text) + '"'


def quote(text: str) -> str:
    return text if _BARE_WORD.fullmatch(text) else quoted(text)


def _word(m: re.Match[str]) -> Word:
    if m[1]:
        return Word(m[1], unescape(m[2]), m[0])
    bare = m[3]
    return Word(bare[0], bare[1:], bare) if bare[0] in SIGILS else Word("", bare, bare)


def tokens(text: str) -> Tokens | Rejected:
    words: list[Word] = []
    rest = text.strip()
    while rest and not rest.startswith(";"):
        if not (m := _WORD.match(rest)):
            return Rejected(f"unexpected text {rest!r}")
        words.append(_word(m))
        rest = rest[m.end() :].lstrip()
    return Tokens(tuple(words), rest[1:].strip())
