import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, auto
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import urlsplit

from subnet_tracker.config import parse_https_url
from subnet_tracker.types import FieldError, IPAddress, ResolverKind

_DNS = re.compile(r"(?i:(udp|tcp|tls))://(?:\[(?P<v6>[^\]]+)\]|(?P<v4>[0-9.]+))(?::(?P<port>[0-9]{1,5}))?")
# the origin of a browser endpoint goes into the CSP header unescaped
_HOST = re.compile(r"[a-z0-9.:-]+")


class DnsTransport(StrEnum):
    UDP = auto()
    TCP = auto()
    TLS = auto()


@dataclass(frozen=True, slots=True)
class DnsEndpoint:
    transport: DnsTransport
    ip: IPAddress
    port: int | None = None  # None is the standard port of the transport

    def __str__(self) -> str:
        host = f"[{self.ip}]" if self.ip.version == 6 else str(self.ip)
        return f"{self.transport}://{host}" if self.port is None else f"{self.transport}://{host}:{self.port}"


def parse_dns_endpoint(text: str) -> DnsEndpoint | FieldError:
    error = FieldError("endpoint", f"invalid endpoint {text!r}, expected udp://, tcp:// or tls:// with an IP address and an optional port")
    if not (m := _DNS.fullmatch(text)) or (m["port"] is not None and not 1 <= int(m["port"]) <= 65535):
        return error
    try:
        ip: IPAddress = IPv6Address(m["v6"]) if m["v6"] else IPv4Address(m["v4"])
    except ValueError:
        return error
    if isinstance(ip, IPv6Address) and (ip.scope_id or ip.ipv4_mapped is not None):
        return error
    return DnsEndpoint(DnsTransport(m[1].lower()), ip, None if m["port"] is None else int(m["port"]))


def https_endpoint(text: str) -> str | FieldError:
    try:
        url = urlsplit(parse_https_url(text))
    except ValueError as e:
        return FieldError("endpoint", str(e))
    if "@" in url.netloc:
        return FieldError("endpoint", f"invalid endpoint {text!r}, userinfo is not allowed")
    if text.isascii() and text.isprintable() and " " not in text and _HOST.fullmatch(url.hostname or ""):
        return text
    return FieldError("endpoint", f"invalid endpoint {text!r}, expected printable ASCII without spaces and a host of letters, digits, dots and dashes or an IP address")


def _dns_endpoint(text: str) -> str | FieldError:
    endpoint = parse_dns_endpoint(text)
    return endpoint if isinstance(endpoint, FieldError) else str(endpoint)


_ENDPOINTS: dict[ResolverKind, Callable[[str], str | FieldError]] = {ResolverKind.DNS: _dns_endpoint, ResolverKind.DOH: https_endpoint, ResolverKind.BROWSER: https_endpoint}


def resolver_endpoint(kind: ResolverKind, text: str | None) -> str | FieldError | None:
    text = (text or "").strip()
    if (parse := _ENDPOINTS.get(kind)) is None:
        return FieldError("endpoint", f"{kind} resolvers have no endpoint") if text else None
    return parse(text) if text else FieldError("endpoint", f"{kind} resolvers need an endpoint")
