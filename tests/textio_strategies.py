from hypothesis import strategies as st

# everything str.splitlines() breaks on: names, descriptions and comments never contain these (design section 12)
LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_char = st.one_of(st.sampled_from(' \t\u00a0\u3000\x1f"\\;#@=![]'), st.characters(exclude_categories=["Cs"], exclude_characters=LINE_BREAKS))


def one_line_strings(min_size: int = 0, max_size: int = 30) -> st.SearchStrategy[str]:
    return st.text(_char, min_size=min_size, max_size=max_size)


def trimmed_text() -> st.SearchStrategy[str]:
    return one_line_strings().map(str.strip)
