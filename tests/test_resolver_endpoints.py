from ipaddress import IPv4Address, IPv6Address

import pytest
from hypothesis import given
from hypothesis import strategies as st

from subnet_tracker.core.resolver_endpoints import DnsEndpoint, DnsTransport, https_endpoint, parse_dns_endpoint, resolver_endpoint
from subnet_tracker.types import FieldError, IPAddress, ResolverKind


def dns_error(text: str) -> FieldError:
    return FieldError("endpoint", f"invalid endpoint {text!r}, expected udp://, tcp:// or tls:// with an IP address and an optional port")


def host_error(text: str) -> FieldError:
    return FieldError("endpoint", f"invalid endpoint {text!r}, expected printable ASCII without spaces and a host of letters, digits, dots and dashes or an IP address")


@pytest.mark.parametrize(
    ("text", "endpoint", "normalized"),
    [
        ("udp://1.1.1.1", DnsEndpoint(DnsTransport.UDP, IPv4Address("1.1.1.1")), "udp://1.1.1.1"),
        ("tcp://[2606:4700:4700::1111]", DnsEndpoint(DnsTransport.TCP, IPv6Address("2606:4700:4700::1111")), "tcp://[2606:4700:4700::1111]"),
        ("tls://1.1.1.1:853", DnsEndpoint(DnsTransport.TLS, IPv4Address("1.1.1.1"), 853), "tls://1.1.1.1:853"),
        ("udp://[2606:4700:4700:0:0:0:0:1111]:5353", DnsEndpoint(DnsTransport.UDP, IPv6Address("2606:4700:4700::1111"), 5353), "udp://[2606:4700:4700::1111]:5353"),
    ],
)
def test_dns_endpoint_is_parsed_and_normalized(text: str, endpoint: DnsEndpoint, normalized: str) -> None:
    assert parse_dns_endpoint(text) == endpoint
    assert str(endpoint) == normalized


@pytest.mark.parametrize(
    "text",
    [
        "1.1.1.1",
        "dot://1.1.1.1",
        "UDP://1.1.1.1",
        "udp://dns.google",
        "udp://2606:4700::1111",
        "udp://[1.1.1.1]",
        "udp://999.1.1.1",
        "udp://1.1.1.1:0",
        "udp://1.1.1.1:65536",
        "udp://1.1.1.1/",
        "udp://1.1.1.1:",
    ],
)
def test_invalid_dns_endpoint_is_rejected(text: str) -> None:
    assert parse_dns_endpoint(text) == dns_error(text)


@given(st.sampled_from(DnsTransport), st.ip_addresses(), st.none() | st.integers(1, 65535))
def test_dns_endpoint_text_round_trips(transport: DnsTransport, ip: IPAddress, port: int | None) -> None:
    endpoint = DnsEndpoint(transport, ip, port)
    assert parse_dns_endpoint(str(endpoint)) == endpoint


@pytest.mark.parametrize(
    "text", ["https://cloudflare-dns.com/dns-query", "https://dns.google/resolve", "https://[2606:4700::1111]/dns-query", "https://1.1.1.1:8443/dns-query", "https://xn--e1afmkfd.xn--p1ai/q?a=1"]
)
def test_https_endpoint_is_accepted_as_given(text: str) -> None:
    assert https_endpoint(text) == text


def test_non_https_endpoint_is_rejected() -> None:
    for text in ("http://dns.google/resolve", "https:///dns-query", "udp://1.1.1.1"):
        assert https_endpoint(text) == FieldError("endpoint", f"invalid endpoint {text!r}, expected an https:// URL")


@pytest.mark.parametrize(
    "text", ["https://a;b.com/", "https://dns.google/a b", "https://dns.google/\ta", "https://dns.google/\x7f", "https://пример.рф/", "https://a'b.com/", "https://dns.google%0a/"]
)
def test_https_endpoint_with_unsafe_characters_is_rejected(text: str) -> None:
    assert https_endpoint(text) == host_error(text)


@pytest.mark.parametrize(
    ("kind", "text", "result"),
    [
        (ResolverKind.SYSTEM, None, None),
        (ResolverKind.SYSTEM, " ", None),
        (ResolverKind.AGENT, None, None),
        (ResolverKind.SYSTEM, "udp://1.1.1.1", FieldError("endpoint", "system resolvers have no endpoint")),
        (ResolverKind.AGENT, "udp://1.1.1.1", FieldError("endpoint", "agent resolvers have no endpoint")),
        (ResolverKind.DNS, " udp://[2606:4700:4700:0::1111] ", "udp://[2606:4700:4700::1111]"),
        (ResolverKind.DNS, None, FieldError("endpoint", "dns resolvers need an endpoint")),
        (ResolverKind.DOH, "", FieldError("endpoint", "doh resolvers need an endpoint")),
        (ResolverKind.BROWSER, " \t", FieldError("endpoint", "browser resolvers need an endpoint")),
        (ResolverKind.DNS, "https://dns.google/resolve", dns_error("https://dns.google/resolve")),
        (ResolverKind.DOH, "udp://1.1.1.1", FieldError("endpoint", "invalid endpoint 'udp://1.1.1.1', expected an https:// URL")),
        (ResolverKind.BROWSER, " https://dns.google/resolve ", "https://dns.google/resolve"),
    ],
)
def test_endpoint_follows_resolver_kind(kind: ResolverKind, text: str | None, result: str | FieldError | None) -> None:
    assert resolver_endpoint(kind, text) == result


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        (ResolverKind.SYSTEM, None),
        (ResolverKind.DNS, "udp://1.1.1.1"),
        (ResolverKind.DNS, "udp://8.8.8.8"),
        (ResolverKind.DNS, "udp://[2606:4700:4700::1111]"),
        (ResolverKind.DOH, "https://cloudflare-dns.com/dns-query"),
        (ResolverKind.BROWSER, "https://cloudflare-dns.com/dns-query"),
    ],
)
def test_resolvers_of_the_first_install_are_valid(kind: ResolverKind, text: str | None) -> None:
    assert resolver_endpoint(kind, text) == text
