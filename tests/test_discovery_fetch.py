import socket
import threading
from collections.abc import Callable, Mapping
from ipaddress import ip_address
from time import monotonic

import httpx
import pytest

from scan_fakes import FakeDns, FakeExchange
from subnet_tracker.discovery.fetch import (
    OVERALL_SECONDS,
    ExchangeError,
    FetchedPage,
    FetchError,
    HttpResponse,
    HttpxExchange,
    ResponseTooLargeError,
    fetch_pages,
    refused_address,
    system_dns,
)
from subnet_tracker.types import Bytes, IPAddress, Seconds

PUBLIC = {"example.com": ("1.2.3.4",)}


def page(url: str, body: bytes = b"ok", status: int = 200, headers: tuple[tuple[str, str], ...] = ()) -> HttpResponse:
    return HttpResponse(status, headers, body)


def run(
    urls: list[str],
    dns: FakeDns | None = None,
    exchange: FakeExchange | None = None,
    *,
    timeout: int = 10,
    max_bytes: int = 1000,
    user_agent: str = "agent",
    clock: Callable[[], float] = monotonic,
) -> tuple[FetchedPage | FetchError, ...]:
    return fetch_pages(urls, dns=dns or FakeDns(PUBLIC), exchange=exchange or FakeExchange({}), timeout=Seconds(timeout), max_bytes=Bytes(max_bytes), user_agent=user_agent, clock=clock)


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("10.1.2.3", "private"),
        ("192.168.1.1", "private"),
        ("172.16.5.1", "private"),
        ("100.64.1.1", "private"),
        ("0.0.0.0", "private"),
        ("224.0.0.1", "private"),
        ("192.0.2.1", "private"),
        ("fd00::1", "private"),
        ("::", "private"),
        ("127.0.0.1", "loopback"),
        ("127.0.0.2", "loopback"),
        ("::1", "loopback"),
        ("::ffff:127.0.0.1", "loopback"),
        ("169.254.169.254", "link-local"),
        ("fe80::1", "link-local"),
        ("::ffff:169.254.1.1", "link-local"),
        ("::ffff:10.0.0.1", "private"),
        ("8.8.8.8", None),
        ("1.2.3.4", None),
        ("2001:4860:4860::8888", None),
        ("::ffff:8.8.8.8", None),
    ],
)
def test_refused_address(address: str, reason: str | None) -> None:
    assert refused_address(ip_address(address)) == reason


def test_public_host_is_pinned_and_carries_the_user_agent() -> None:
    exchange = FakeExchange({"https://example.com/a": page("https://example.com/a", b"<p>hi</p>")})
    result = run(["https://example.com/a"], exchange=exchange, user_agent="ScanTest")
    assert result == (FetchedPage("https://example.com/a", ("https://example.com/a",), (), "<p>hi</p>"),)
    assert exchange.sent == [exchange.sent[0]]
    assert exchange.sent[0].pin == ip_address("1.2.3.4")
    assert exchange.sent[0].user_agent == "ScanTest"
    assert exchange.sent[0].timeout == 10
    assert exchange.sent[0].max_bytes == 1000


def test_fragment_is_not_requested() -> None:
    exchange = FakeExchange({"https://example.com/a": page("https://example.com/a")})
    result = run(["https://example.com/a#section"], exchange=exchange)
    assert isinstance(result[0], FetchedPage)
    assert result[0].url == "https://example.com/a#section"
    assert exchange.sent[0].url == "https://example.com/a"


def test_public_ip_literal_skips_dns() -> None:
    dns = FakeDns({})
    exchange = FakeExchange({"http://1.2.3.4/x": page("http://1.2.3.4/x", b"literal")})
    result = run(["http://1.2.3.4/x"], dns, exchange)
    assert isinstance(result[0], FetchedPage)
    assert result[0].body == "literal"
    assert dns.calls == []
    assert exchange.sent[0].pin == ip_address("1.2.3.4")


def test_idn_is_resolved_as_punycode() -> None:
    dns = FakeDns({"xn--e1afmkfd.xn--p1ai": ("1.2.3.4",)})
    exchange = FakeExchange({"https://пример.рф/x": page("https://пример.рф/x", "привет".encode())})
    result = run(["https://пример.рф/x"], dns, exchange)
    assert dns.calls == ["xn--e1afmkfd.xn--p1ai"]
    assert isinstance(result[0], FetchedPage)
    assert result[0].body == "привет"


def test_charset_is_honored_and_unknown_charset_falls_back_to_utf_8() -> None:
    exchange = FakeExchange(
        {
            "https://example.com/latin": page("https://example.com/latin", "café".encode("latin-1"), headers=(("content-type", "text/html; charset=iso-8859-1"),)),
            "https://example.com/bad": page("https://example.com/bad", "привет".encode(), headers=(("content-type", "text/html; charset=not-a-charset"),)),
        }
    )
    latin, bad = run(["https://example.com/latin", "https://example.com/bad"], exchange=exchange)
    assert isinstance(latin, FetchedPage)
    assert latin.body == "café"
    assert isinstance(bad, FetchedPage)
    assert bad.body == "привет"


def test_http_error_status_still_returns_the_body() -> None:
    exchange = FakeExchange({"https://example.com/": page("https://example.com/", b"missing", status=404)})
    result = run(["https://example.com/"], exchange=exchange)
    assert isinstance(result[0], FetchedPage)
    assert result[0].body == "missing"


@pytest.mark.parametrize(
    ("url", "records", "reason", "address"),
    [
        ("http://10.1.2.3/", {}, "private", "10.1.2.3"),
        ("http://192.168.0.1/", {}, "private", "192.168.0.1"),
        ("http://172.16.0.1/", {}, "private", "172.16.0.1"),
        ("http://127.0.0.1/", {}, "loopback", "127.0.0.1"),
        ("http://[::1]/", {}, "loopback", "::1"),
        ("http://169.254.169.254/", {}, "link-local", "169.254.169.254"),
        ("http://[fe80::1]/", {}, "link-local", "fe80::1"),
        ("http://[::ffff:127.0.0.1]/", {}, "loopback", "::ffff:127.0.0.1"),
        ("http://[fd00::1]/", {}, "private", "fd00::1"),
        ("https://secret.example/", {"secret.example": ("192.168.0.5",)}, "private", "192.168.0.5"),
        ("https://local.example/", {"local.example": ("127.0.0.1",)}, "loopback", "127.0.0.1"),
        ("https://link.example/", {"link.example": ("169.254.1.1",)}, "link-local", "169.254.1.1"),
        ("https://mixed.example/", {"mixed.example": ("8.8.8.8", "10.0.0.1")}, "private", "10.0.0.1"),
    ],
)
def test_blocked_destination_is_not_fetched(url: str, records: dict[str, tuple[str, ...]], reason: str, address: str) -> None:
    dns = FakeDns(records)
    exchange = FakeExchange({})
    result = run([url], dns, exchange)
    assert result == (FetchError(url, f"{reason} address {address}"),)
    assert exchange.sent == []


def test_unresolved_name_is_an_error_and_unsupported_schemes_are_not_resolved() -> None:
    dns = FakeDns({})
    exchange = FakeExchange({})
    missing, file_url, ftp = run(["https://missing.example/", "file:///etc/passwd", "ftp://files.example/pub"], dns, exchange)
    assert missing == FetchError("https://missing.example/", "name not resolved")
    assert file_url == FetchError("file:///etc/passwd", "unsupported URL")
    assert ftp == FetchError("ftp://files.example/pub", "unsupported URL")
    assert dns.calls == ["missing.example"]
    assert exchange.sent == []


def test_redirect_to_another_host_is_resolved_again() -> None:
    dns = FakeDns({"example.com": ("1.2.3.4",), "cdn.example.net": ("8.8.8.8",)})
    exchange = FakeExchange(
        {
            "https://example.com/a": page("https://example.com/a", b"", status=302, headers=(("location", "https://cdn.example.net/b"),)),
            "https://cdn.example.net/b": page("https://cdn.example.net/b", b"landed"),
        }
    )
    result = run(["https://example.com/a"], dns, exchange, user_agent="ScanTest")
    assert isinstance(result[0], FetchedPage)
    assert result[0].chain == ("https://example.com/a", "https://cdn.example.net/b")
    assert result[0].body == "landed"
    assert dns.calls == ["example.com", "cdn.example.net"]
    assert [item.pin for item in exchange.sent] == [ip_address("1.2.3.4"), ip_address("8.8.8.8")]
    assert [item.user_agent for item in exchange.sent] == ["ScanTest", "ScanTest"]


def test_redirect_to_a_private_address_stops_before_the_second_request() -> None:
    dns = FakeDns({"example.com": ("1.2.3.4",)})
    exchange = FakeExchange({"https://example.com/a": page("https://example.com/a", b"", status=302, headers=(("location", "http://127.0.0.1/secret"),))})
    result = run(["https://example.com/a"], dns, exchange)
    assert result == (FetchError("https://example.com/a", "loopback address 127.0.0.1"),)
    assert [item.url for item in exchange.sent] == ["https://example.com/a"]


def test_relative_redirect_is_joined_and_the_same_host_is_resolved_again() -> None:
    dns = FakeDns(PUBLIC)
    exchange = FakeExchange(
        {
            "https://example.com/a/b": page("https://example.com/a/b", b"", status=302, headers=(("location", "/next"),)),
            "https://example.com/next": page("https://example.com/next", b"there"),
        }
    )
    result = run(["https://example.com/a/b"], dns, exchange)
    assert isinstance(result[0], FetchedPage)
    assert result[0].chain == ("https://example.com/a/b", "https://example.com/next")
    assert dns.calls == ["example.com", "example.com"]


def test_protocol_relative_redirect_keeps_the_scheme() -> None:
    dns = FakeDns({"example.com": ("1.2.3.4",), "other.test": ("8.8.8.8",)})
    exchange = FakeExchange(
        {
            "https://example.com/a": page("https://example.com/a", b"", status=301, headers=(("Location", "//other.test/x"),)),
            "https://other.test/x": page("https://other.test/x", b"ok"),
        }
    )
    result = run(["https://example.com/a"], dns, exchange)
    assert isinstance(result[0], FetchedPage)
    assert result[0].chain == ("https://example.com/a", "https://other.test/x")


def test_redirect_to_a_non_http_scheme_is_refused() -> None:
    exchange = FakeExchange({"https://example.com/a": page("https://example.com/a", b"big", status=302, headers=(("location", "file:///etc/passwd"),))})
    result = run(["https://example.com/a"], exchange=exchange)
    assert result == (FetchError("https://example.com/a", "unsupported URL"),)
    assert len(exchange.sent) == 1


def test_redirect_without_location_is_an_error() -> None:
    exchange = FakeExchange({"https://example.com/a": page("https://example.com/a", b"", status=302)})
    result = run(["https://example.com/a"], exchange=exchange)
    assert result == (FetchError("https://example.com/a", "redirect without location"),)


def test_five_redirects_reach_the_page_and_the_sixth_does_not() -> None:
    ok = {f"https://example.com/{i}": page(f"https://example.com/{i}", b"", status=302, headers=(("location", f"/{i + 1}"),)) for i in range(5)}
    ok["https://example.com/5"] = page("https://example.com/5", b"done")
    done = run(["https://example.com/0"], exchange=FakeExchange(ok))
    assert isinstance(done[0], FetchedPage)
    assert done[0].chain == tuple(f"https://example.com/{i}" for i in range(6))
    assert done[0].body == "done"
    too_many = {f"https://example.com/{i}": page(f"https://example.com/{i}", b"", status=302, headers=(("location", f"/{i + 1}"),)) for i in range(6)}
    exchange = FakeExchange(too_many)
    refused = run(["https://example.com/0"], exchange=exchange)
    assert refused == (FetchError("https://example.com/0", "more than 5 redirects"),)
    assert [item.url for item in exchange.sent] == [f"https://example.com/{i}" for i in range(6)]


def test_body_and_content_length_over_the_limit_are_errors() -> None:
    exchange = FakeExchange(
        {
            "https://example.com/body": page("https://example.com/body", b"12345"),
            "https://example.com/len": page("https://example.com/len", b"hi", headers=(("content-length", "100"),)),
            "https://example.com/hop": page("https://example.com/hop", b"12345", status=302, headers=(("location", "/next"), ("content-length", "5"))),
        }
    )
    body, length, hop = run(["https://example.com/body", "https://example.com/len", "https://example.com/hop"], exchange=exchange, max_bytes=4)
    assert body == FetchError("https://example.com/body", "response exceeds 4B")
    assert length == FetchError("https://example.com/len", "response exceeds 4B")
    assert hop == FetchError("https://example.com/hop", "response exceeds 4B")
    assert [item.url for item in exchange.sent] == ["https://example.com/body", "https://example.com/len", "https://example.com/hop"]


def test_exchange_timeout_uses_the_configured_limit() -> None:
    exchange = FakeExchange({"https://example.com/": TimeoutError()})
    result = run(["https://example.com/"], exchange=exchange, timeout=10)
    assert result == (FetchError("https://example.com/", "timeout after 10s"),)


def test_overall_deadline_stops_a_redirect_and_a_late_start() -> None:
    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()

    class Advance(FakeExchange):
        def send(self, url: str, headers: Mapping[str, str], pin: IPAddress, timeout: float, max_bytes: int) -> HttpResponse:
            sent = super().send(url, headers, pin, timeout, max_bytes)
            clock.now = 27
            return sent

    exchange = Advance(
        {
            "https://example.com/a": page("https://example.com/a", b"", status=302, headers=(("location", "/b"),)),
            "https://example.com/b": page("https://example.com/b", b"ok"),
        }
    )
    result = run(["https://example.com/a"], exchange=exchange, clock=clock)
    assert isinstance(result[0], FetchedPage)
    assert [item.timeout for item in exchange.sent] == [10, 3]

    class Jump:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> float:
            self.calls += 1
            return 0 if self.calls == 1 else OVERALL_SECONDS + 1

    late_exchange = FakeExchange({"https://example.com/": page("https://example.com/")})
    result = run(["https://example.com/"], exchange=late_exchange, clock=Jump())
    assert result == (FetchError("https://example.com/", f"timeout after {OVERALL_SECONDS}s"),)
    assert late_exchange.sent == []


def test_pages_are_fetched_in_parallel() -> None:
    barrier = threading.Barrier(2, timeout=2)

    class Waiting(FakeExchange):
        def send(self, url: str, headers: Mapping[str, str], pin: IPAddress, timeout: float, max_bytes: int) -> HttpResponse:
            barrier.wait()
            return super().send(url, headers, pin, timeout, max_bytes)

    exchange = Waiting({"https://a.example/": page("https://a.example/"), "https://b.example/": page("https://b.example/")})
    dns = FakeDns({"a.example": ("1.2.3.4",), "b.example": ("8.8.8.8",)})
    result = run(["https://a.example/", "https://b.example/"], dns, exchange)
    assert [type(item) for item in result] == [FetchedPage, FetchedPage]


def test_first_address_is_the_pin_when_every_address_is_public() -> None:
    dns = FakeDns({"example.com": ("1.2.3.4", "8.8.8.8")})
    exchange = FakeExchange({"https://example.com/": page("https://example.com/")})
    run(["https://example.com/"], dns, exchange)
    assert exchange.sent[0].pin == ip_address("1.2.3.4")


def test_httpx_exchange_connects_only_to_the_pinned_address() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["port"] = request.url.port
        seen["header"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, content=b"<p>ok</p>")

    exchange = HttpxExchange(httpx.MockTransport(handler))
    response = exchange.send("https://example.com:8443/a", {"user-agent": "TestAgent"}, ip_address("1.2.3.4"), 10, 1000)
    assert seen == {"host": "1.2.3.4", "port": 8443, "header": "example.com:8443", "sni": "example.com", "ua": "TestAgent"}
    assert response.status == 200
    assert response.body == b"<p>ok</p>"


def test_httpx_exchange_refuses_a_private_pin_and_stops_past_max_bytes() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"abcdef")

    exchange = HttpxExchange(httpx.MockTransport(handler))
    with pytest.raises(ExchangeError, match="loopback"):
        exchange.send("http://example.com/", {}, ip_address("127.0.0.1"), 5, 100)
    assert not called
    with pytest.raises(ResponseTooLargeError):
        exchange.send("https://example.com/", {"user-agent": "T"}, ip_address("1.2.3.4"), 5, 4)


def test_httpx_exchange_turns_a_timeout_into_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TimeoutError):
        HttpxExchange(httpx.MockTransport(handler)).send("https://example.com/", {}, ip_address("1.2.3.4"), 5, 100)


def test_system_dns_reads_getaddrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(host: str, port: object, *_args: object, **_kwargs: object) -> list[object]:
        assert host == "example.com"
        assert port is None
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 0, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", 0, 0, 1)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert system_dns("example.com") == (ip_address("1.2.3.4"), ip_address("2001:db8::1"), ip_address("fe80::1"))


def test_system_dns_returns_nothing_when_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_args: object, **_kwargs: object) -> list[object]:
        raise socket.gaierror(1, "fail")

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert system_dns("example.com") == ()
