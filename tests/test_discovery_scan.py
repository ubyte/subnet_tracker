from scan_fakes import FakeDns, FakeExchange
from subnet_tracker.core.settings import DiscoverySettings
from subnet_tracker.discovery.extract import ScanRole
from subnet_tracker.discovery.fetch import HttpResponse
from subnet_tracker.discovery.scan import ScanError, ScanResult, scan
from subnet_tracker.types import Bytes, DomainName, Seconds


def settings() -> DiscoverySettings:
    return DiscoverySettings(timeout=Seconds(10), max_bytes=Bytes(1000), user_agent="ScanTest", ignore=("w3.org", "schema.org", "ogp.me"))


def response(body: bytes, status: int = 200, headers: tuple[tuple[str, str], ...] = ()) -> HttpResponse:
    return HttpResponse(status, headers, body)


def test_scan_classifies_pages_without_following_links() -> None:
    first = b"""\
<a href="https://static.example.com/a.js">s</a>
<a href="https://fonts.gstatic.com/a.woff">f</a>
<script src="https://cdn.jsdelivr.net/x.js"></script>
<a href="https://ssl.gstatic.com/b">g</a>
<a href="https://www.w3.org/TR">w</a>
"""
    second = b'<a href="https://api.other.org/v"></a>'
    urls = ["https://www.example.com/some/page", "https://other.org/x"]
    dns = FakeDns({"www.example.com": ("1.2.3.4",), "other.org": ("8.8.8.8",)})
    exchange = FakeExchange(
        {
            urls[0]: response(first, headers=(("content-security-policy", "script-src https://csp.example.com"),)),
            urls[1]: response(second),
        }
    )
    result = scan(urls, existing={DomainName("api.other.org")}, groups=set(), settings=settings(), dns=dns, exchange=exchange)
    assert result.errors == ()
    assert result.ignored == ("w3.org",)
    assert result.group_name == "example.com +1"
    assert [(item.name, item.role, item.exists, item.selected, item.sources) for item in result.candidates] == [
        ("www.example.com", ScanRole.SAME, False, True, ("page host",)),
        ("csp.example.com", ScanRole.SAME, False, True, ("CSP",)),
        ("static.example.com", ScanRole.SAME, False, True, ("a href",)),
        ("other.org", ScanRole.SAME, False, True, ("page host",)),
        ("api.other.org", ScanRole.SAME, True, False, ("a href",)),
        ("fonts.gstatic.com", ScanRole.THIRD, False, False, ("a href",)),
        ("ssl.gstatic.com", ScanRole.THIRD, False, False, ("a href",)),
        ("cdn.jsdelivr.net", ScanRole.THIRD, False, False, ("script src",)),
    ]
    assert {item.url for item in exchange.sent} == set(urls)
    assert {item.user_agent for item in exchange.sent} == {"ScanTest"}


def test_redirect_chain_names_hosts_but_only_the_submitted_registrable_domain_is_selected() -> None:
    dns = FakeDns({"short.example": ("1.2.3.4",), "example.com": ("8.8.8.8",)})
    exchange = FakeExchange(
        {
            "https://short.example/x": response(b"", status=302, headers=(("location", "https://example.com/land"),)),
            "https://example.com/land": response(b"<script src='https://static.example.com/a.js'></script>"),
        }
    )
    result = scan(["https://short.example/x"], existing=set(), groups=set(), settings=settings(), dns=dns, exchange=exchange)
    assert result.group_name == "short.example"
    assert [(item.name, item.role, item.sources, item.selected) for item in result.candidates] == [
        ("short.example", ScanRole.SAME, ("page host",), True),
        ("example.com", ScanRole.THIRD, ("redirect",), False),
        ("static.example.com", ScanRole.THIRD, ("script src",), False),
    ]


def test_redirect_on_the_submitted_registrable_domain_is_selected() -> None:
    dns = FakeDns({"www.example.com": ("1.2.3.4",), "static.example.com": ("8.8.8.8",)})
    exchange = FakeExchange(
        {
            "https://www.example.com/x": response(b"", status=302, headers=(("location", "https://static.example.com/land"),)),
            "https://static.example.com/land": response(b"<script src='https://cdn.example.com/a.js'></script>"),
        }
    )
    result = scan(["https://www.example.com/x"], existing=set(), groups=set(), settings=settings(), dns=dns, exchange=exchange)
    assert result.group_name == "example.com"
    assert [(item.name, item.role, item.sources, item.selected) for item in result.candidates] == [
        ("www.example.com", ScanRole.SAME, ("page host",), True),
        ("static.example.com", ScanRole.SAME, ("redirect",), True),
        ("cdn.example.com", ScanRole.SAME, ("script src",), True),
    ]


def test_private_redirect_target_does_not_drop_the_other_page() -> None:
    dns = FakeDns({"evil.example": ("10.1.2.3",), "ok.example": ("1.2.3.4",)})
    exchange = FakeExchange({"https://ok.example/": response(b"<a href='https://static.ok.example/'></a>")})
    result = scan(["https://evil.example/", "https://ok.example/"], existing=set(), groups=set(), settings=settings(), dns=dns, exchange=exchange)
    assert result.errors == (ScanError("https://evil.example/", "private address 10.1.2.3"),)
    assert [item.name for item in result.candidates] == ["ok.example", "static.ok.example"]
    assert [item.url for item in exchange.sent] == ["https://ok.example/"]


def test_timeout_is_reported_for_that_url_only() -> None:
    dns = FakeDns({"broken.example": ("1.2.3.4",), "ok.example": ("8.8.8.8",)})
    exchange = FakeExchange({"https://broken.example/": TimeoutError(), "https://ok.example/": response(b"")})
    result = scan(["https://broken.example/", "https://ok.example/"], existing=set(), groups=set(), settings=settings(), dns=dns, exchange=exchange)
    assert result.errors == (ScanError("https://broken.example/", "timeout after 10s"),)
    assert [item.name for item in result.candidates] == ["ok.example"]


def test_more_than_20_urls_are_not_fetched() -> None:
    urls = [f"https://s{i}.example.com/" for i in range(21)]
    dns = FakeDns({})
    exchange = FakeExchange({})
    result = scan(urls, existing=set(), groups=set(), settings=settings(), dns=dns, exchange=exchange)
    assert result == ScanResult((), (), (ScanError("", "at most 20 URLs"),), "example.com")
    assert dns.calls == []
    assert exchange.sent == []


def test_twenty_urls_are_fetched() -> None:
    urls = [f"https://s{i}.example.com/" for i in range(20)]
    dns = FakeDns({f"s{i}.example.com": ("1.2.3.4",) for i in range(20)})
    exchange = FakeExchange({url: response(b"") for url in urls})
    result = scan(urls, existing=set(), groups=set(), settings=settings(), dns=dns, exchange=exchange)
    assert result.errors == ()
    assert len(result.candidates) == 20
    assert len(exchange.sent) == 20


def test_empty_scan() -> None:
    result = scan([], existing=set(), groups=set(), settings=settings(), dns=FakeDns({}), exchange=FakeExchange({}))
    assert result == ScanResult()
