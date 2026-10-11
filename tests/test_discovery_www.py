import pytest
from hypothesis import given
from hypothesis import strategies as st

from subnet_tracker.discovery.www import DomainNames, WwwPair, domain_names, pair_name

ALL, REGISTRABLE, OFF = WwwPair.ALL, WwwPair.REGISTRABLE, WwwPair.OFF


def test_modes_match_setting_values() -> None:
    assert [m.value for m in WwwPair] == ["all", "registrable", "off"]


@pytest.mark.parametrize(
    ("name", "mode", "pair"),
    [
        ("example.com", ALL, "www.example.com"),
        ("www.example.com", ALL, "example.com"),
        ("api.example.com", ALL, "www.api.example.com"),
        ("www.api.example.com", ALL, "api.example.com"),
        ("www.www.example.com", ALL, "www.example.com"),
        ("example.com", REGISTRABLE, "www.example.com"),
        ("www.example.com", REGISTRABLE, "example.com"),
        ("api.example.com", REGISTRABLE, None),
        ("www.api.example.com", REGISTRABLE, None),
        ("www.www.example.com", REGISTRABLE, None),
        ("example.co.uk", REGISTRABLE, "www.example.co.uk"),
        ("www.example.co.uk", REGISTRABLE, "example.co.uk"),
        ("example.com", OFF, None),
        ("www.example.com", OFF, None),
    ],
)
def test_pair_name(name: str, mode: WwwPair, pair: str | None) -> None:
    assert pair_name(name, mode) == pair


@pytest.mark.parametrize("mode", list(WwwPair))
@pytest.mark.parametrize("name", ["com", "www.com", "co.uk", "www.co.uk", "github.io", "www.github.io", "localhost", "www.local", "xn--p1ai", "www.xn--p1ai", "www.foo.ck"])
def test_public_suffix_is_never_paired(name: str, mode: WwwPair) -> None:
    assert pair_name(name, mode) is None


@pytest.mark.parametrize("mode", [ALL, REGISTRABLE])
@pytest.mark.parametrize(
    ("name", "pair"),
    [
        ("user.github.io", "www.user.github.io"),
        ("www.user.github.io", "user.github.io"),
        ("example.local", "www.example.local"),
        ("xn--e1afmkfd.xn--p1ai", "www.xn--e1afmkfd.xn--p1ai"),
        ("www.xn--e1afmkfd.xn--p1ai", "xn--e1afmkfd.xn--p1ai"),
        ("city.kawasaki.jp", "www.city.kawasaki.jp"),
    ],
)
def test_registrable_names_are_paired(name: str, pair: str, mode: WwwPair) -> None:
    assert pair_name(name, mode) == pair


@pytest.mark.parametrize(("length", "pair"), [(249, True), (250, False)])
def test_pair_over_253_characters_is_invalid(length: int, pair: bool) -> None:
    name = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * (length - 196), "com"])
    assert len(name) == length
    assert pair_name(name, ALL) == ("www." + name if pair else None)


@pytest.mark.parametrize(
    ("canonical", "mode", "names"),
    [
        ("example.com", ALL, DomainNames("example.com", "www.example.com")),
        ("www.example.com", ALL, DomainNames("www.example.com", "example.com")),
        ("api.example.com", REGISTRABLE, DomainNames("api.example.com", None)),
        ("www.example.com", OFF, DomainNames("www.example.com", None)),
        ("www.co.uk", ALL, DomainNames("www.co.uk", None)),
    ],
)
def test_domain_names_keep_entered_name_canonical(canonical: str, mode: WwwPair, names: DomainNames) -> None:
    assert domain_names(canonical, mode) == names


def test_domain_names_lists_canonical_first() -> None:
    assert domain_names("www.example.com", ALL).all == ("www.example.com", "example.com")
    assert domain_names("www.example.com", OFF).all == ("www.example.com",)


_label = st.from_regex(r"[a-z0-9]([a-z0-9-]{0,10}[a-z0-9])?", fullmatch=True)
_suffix = st.sampled_from(["com", "co.uk", "github.io", "xn--p1ai", "local"])
_names = st.builds(lambda www, labels, suffix: "www." * www + ".".join([*labels, suffix]), st.integers(0, 2), st.lists(_label, max_size=3), _suffix)


@given(_names, st.sampled_from(list(WwwPair)))
def test_pair_differs_by_one_leading_www(name: str, mode: WwwPair) -> None:
    pair = pair_name(name, mode)
    assert pair is None or pair == "www." + name or name == "www." + pair


@given(_names, st.sampled_from(list(WwwPair)))
def test_pair_of_pair_is_the_name_unless_double_www(name: str, mode: WwwPair) -> None:
    pair = pair_name(name, mode)
    if pair is not None and not name.startswith("www.www."):
        assert pair_name(pair, mode) == name


@given(_names)
def test_registrable_pairs_are_a_subset_of_all(name: str) -> None:
    pair = pair_name(name, REGISTRABLE)
    assert pair is None or pair == pair_name(name, ALL)
