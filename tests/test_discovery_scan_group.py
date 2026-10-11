import pytest

from subnet_tracker.discovery.scan_group import registrable_domain, scan_group


@pytest.mark.parametrize(
    ("hosts", "taken", "name"),
    [
        (["www.example.com"], frozenset(), "example.com"),
        (["www.example.com", "cdn.example.com"], frozenset(), "example.com"),
        (["blog.example.co.uk"], frozenset(), "example.co.uk"),
        (["www.example.co.uk", "example.co.uk"], frozenset(), "example.co.uk"),
        (["www.co.uk"], frozenset(), "www.co.uk"),
        (["co.uk"], frozenset(), ""),
        (["foo.github.io", "bar.github.io"], frozenset(), "foo.github.io +1"),
        (["example.com", "other.org", "third.net"], frozenset(), "example.com +2"),
        (["example.com", "www.example.com", "other.org"], frozenset(), "example.com +1"),
        (["other.org", "example.com"], frozenset(), "other.org +1"),
        (["example.com"], frozenset({"example.com"}), "example.com (2)"),
        (["example.com"], frozenset({"example.com", "example.com (2)"}), "example.com (3)"),
        (["example.com", "other.org"], frozenset({"example.com +1"}), "example.com +1 (2)"),
        ([], frozenset(), ""),
        (["1.2.3.4", "localhost"], frozenset(), ""),
        (["пример.рф"], frozenset(), "xn--e1afmkfd.xn--p1ai"),
        (["EXAMPLE.COM."], frozenset(), "example.com"),
    ],
)
def test_scan_group_names(hosts: list[str], taken: frozenset[str], name: str) -> None:
    assert scan_group(hosts, taken) == name


def test_long_registrable_domain_is_cut_to_the_group_name_limit() -> None:
    host = "a" * 63 + "." + "b" * 40
    assert registrable_domain(host) == host
    assert scan_group([host], ()) == host[:100]
    assert scan_group([host], {host[:100]}) == host[:96] + " (2)"
