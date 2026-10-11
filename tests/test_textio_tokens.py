import pytest
from hypothesis import given

from subnet_tracker.textio.tokens import Tokens, quote, quoted, tokens
from textio_strategies import LINE_BREAKS, one_line_strings


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Google", "Google"),
        ("example.com+2", "example.com+2"),
        ("#tag@group", "#tag@group"),
        ("back\\slash", "back\\slash"),
        ("two words", '"two words"'),
        ("a\u00a0b", '"a\u00a0b"'),
        ("a\tb", '"a\tb"'),
        ("a\x1fb", '"a\x1fb"'),
        ("semi;colon", '"semi;colon"'),
        ('say "hi"', '"say \\"hi\\""'),
        ('back\\slash "q"', '"back\\\\slash \\"q\\""'),
        ("", '""'),
    ],
)
def test_quote_leaves_bare_words_and_quotes_the_rest(text: str, expected: str) -> None:
    assert quote(text) == expected


def test_quoted_always_quotes() -> None:
    assert quoted("Video") == '"Video"'
    assert quoted('a\\"b') == '"a\\\\\\"b"'


def test_line_breaks_are_exactly_what_splitlines_breaks_on() -> None:
    assert {chr(c) for c in range(0x110000) if len(f"a{chr(c)}b".splitlines()) > 1} == set(LINE_BREAKS)


@given(one_line_strings(min_size=1))
def test_quoted_word_reads_back_as_itself(text: str) -> None:
    match tokens(f"#{quote(text)} @{quote(text)} ; c"):
        case Tokens(words, "c"):
            assert [(w.sigil, w.text) for w in words] == [("#", text), ("@", text)]
        case other:
            pytest.fail(f"unexpected {other!r}")
