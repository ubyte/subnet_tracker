import pytest
from hypothesis import given
from hypothesis import strategies as st

from subnet_tracker.discovery.www import DomainNames, domain_names, pair_name


@pytest.mark.parametrize(
    ("name", "pair"),
    [
        ("example.com", "www.example.com"),
        ("www.example.com", "example.com"),
        ("api.example.com", "www.api.example.com"),
        ("www.api.example.com", "api.example.com"),
        ("www.www.example.com", "www.example.com"),
        ("xn--e1afmkfd.xn--p1ai", "www.xn--e1afmkfd.xn--p1ai"),
        ("www.xn--e1afmkfd.xn--p1ai", "xn--e1afmkfd.xn--p1ai"),
    ],
)
def test_pair_name(name: str, pair: str) -> None:
    assert pair_name(name) == pair


@pytest.mark.parametrize(
    ("name", "pair"),
    [("com", "www.com"), ("www.com", "com"), ("www.co.uk", "co.uk"), ("github.io", "www.github.io"), ("localhost", "www.localhost"), ("www.local", "local"), ("wwwexample.com", "www.wwwexample.com")],
)
def test_pair_is_plain_text_without_suffix_checks(name: str, pair: str) -> None:
    assert pair_name(name) == pair


def test_pair_length_is_left_to_parsing() -> None:
    name = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 54, "com"])
    assert len(pair_name(name)) == 254


@pytest.mark.parametrize(
    ("canonical", "names"),
    [
        ("example.com", DomainNames("example.com", "www.example.com")),
        ("www.example.com", DomainNames("www.example.com", "example.com")),
        ("www.co.uk", DomainNames("www.co.uk", "co.uk")),
    ],
)
def test_domain_names_keep_entered_name_canonical(canonical: str, names: DomainNames) -> None:
    assert domain_names(canonical) == names


def test_domain_names_lists_canonical_first() -> None:
    assert domain_names("www.example.com").all == ("www.example.com", "example.com")


_label = st.from_regex(r"[a-z0-9]([a-z0-9-]{0,10}[a-z0-9])?", fullmatch=True)
_names = st.builds(lambda www, labels: "www." * www + ".".join(labels), st.integers(0, 2), st.lists(_label, min_size=1, max_size=4))


@given(_names)
def test_pair_differs_by_one_leading_www(name: str) -> None:
    pair = pair_name(name)
    assert pair == "www." + name or name == "www." + pair


@given(_names)
def test_pair_of_pair_is_the_name_unless_double_www(name: str) -> None:
    if not name.startswith("www.www."):
        assert pair_name(pair_name(name)) == name
