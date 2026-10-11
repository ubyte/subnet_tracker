import re
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import idna

from subnet_tracker.types import Bytes, IPAddress, Seconds, format_size

MAX_URLS = 20
MAX_REDIRECTS = 5
OVERALL_SECONDS = 30
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_CHARSET = re.compile(r"charset=\s*['\"]?([^'\";\s]+)", re.IGNORECASE)


class ExchangeError(Exception):
    pass


class ResponseTooLargeError(ExchangeError):
    pass


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    chain: tuple[str, ...]
    headers: tuple[tuple[str, str], ...]
    body: str


@dataclass(frozen=True, slots=True)
class FetchError:
    url: str
    message: str


class Dns(Protocol):
    def resolve(self, host: str) -> tuple[IPAddress, ...]: ...


class Exchange(Protocol):
    def send(self, url: str, headers: Mapping[str, str], pin: IPAddress, timeout: float, max_bytes: int) -> HttpResponse: ...


def refused_address(ip: IPAddress) -> str | None:
    if isinstance(ip, IPv6Address) and (mapped := ip.ipv4_mapped) is not None:
        ip = mapped
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_private or not ip.is_global:
        return "private"
    return None


def system_dns(host: str) -> tuple[IPAddress, ...]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return ()
    found: list[IPAddress] = []
    for info in infos:
        address = ip_address(str(info[4][0]).split("%", 1)[0])
        if address not in found:
            found.append(address)
    return tuple(found)


def _host_header(url: httpx.URL) -> str:
    host = f"[{url.host}]" if ":" in url.host else url.host
    if url.port is None or (url.scheme == "http" and url.port == 80) or (url.scheme == "https" and url.port == 443):
        return host
    return f"{host}:{url.port}"


def _read_limited(response: httpx.Response, max_bytes: int) -> HttpResponse:
    length = response.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > max_bytes:
        raise ResponseTooLargeError
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError
        chunks.append(chunk)
    headers = tuple((name.lower(), value) for name, value in response.headers.multi_items())
    return HttpResponse(response.status_code, headers, b"".join(chunks))


class HttpxExchange:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        # No environment proxy: it would connect somewhere other than the checked address.
        self._transport = transport if transport is not None else httpx.HTTPTransport(trust_env=False)

    def send(self, url: str, headers: Mapping[str, str], pin: IPAddress, timeout: float, max_bytes: int) -> HttpResponse:
        if (reason := refused_address(pin)) is not None:
            raise ExchangeError(f"{reason} address {pin}")
        request = httpx.Request("GET", url, headers=dict(headers))
        original = request.url
        request.url = original.copy_with(host=str(pin))
        request.headers["host"] = _host_header(original)
        # The name stays on Host and SNI; the TCP connection uses the address already checked.
        request.extensions = {"sni_hostname": original.host}
        try:
            with httpx.Client(transport=self._transport, follow_redirects=False, timeout=timeout, trust_env=False) as client:
                response = client.send(request, stream=True)
                try:
                    return _read_limited(response, max_bytes)
                finally:
                    response.close()
        except httpx.TimeoutException as error:
            raise TimeoutError(str(error)) from error
        except httpx.HTTPError as error:
            raise ExchangeError(str(error)) from error


def _literal(host: str) -> IPAddress | None:
    try:
        return ip_address(host)
    except ValueError:
        return None


def _target(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError as error:
        raise ExchangeError("unsupported URL") from error
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ExchangeError("unsupported URL")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _addresses(dns: Dns, host: str) -> tuple[IPAddress, ...]:
    literal = _literal(host)
    if literal is not None:
        return (literal,)
    try:
        query = idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError as error:
        raise ExchangeError(str(error)) from error
    return dns.resolve(query)


def _checked_pin(addresses: tuple[IPAddress, ...]) -> IPAddress:
    if not addresses:
        raise ExchangeError("name not resolved")
    for address in addresses:
        if (reason := refused_address(address)) is not None:
            raise ExchangeError(f"{reason} address {address}")
    return addresses[0]


def _header(headers: Sequence[tuple[str, str]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def _content_length(headers: Sequence[tuple[str, str]]) -> int | None:
    value = _header(headers, "content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _next_url(current: str, location: str) -> str:
    try:
        parts = urlsplit(urljoin(current, location))
    except ValueError as error:
        raise ExchangeError("unsupported URL") from error
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ExchangeError("unsupported URL")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _body_text(body: bytes, headers: Sequence[tuple[str, str]]) -> str:
    content_type = _header(headers, "content-type") or ""
    match = _CHARSET.search(content_type)
    charset = match.group(1) if match else "utf-8"
    try:
        return body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


def _fetch_checked(url: str, dns: Dns, exchange: Exchange, timeout: Seconds, max_bytes: Bytes, user_agent: str, clock: Callable[[], float], deadline: float) -> FetchedPage:
    current = _target(url)
    chain: list[str] = []
    followed = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise ExchangeError(f"timeout after {OVERALL_SECONDS}s")
        parts = urlsplit(current)
        pin = _checked_pin(_addresses(dns, parts.hostname or ""))
        try:
            response = exchange.send(current, {"user-agent": user_agent}, pin, min(float(timeout), remaining), max_bytes)
        except TimeoutError:
            raise ExchangeError(f"timeout after {int(timeout)}s") from None
        length = _content_length(response.headers)
        if (length is not None and length > max_bytes) or len(response.body) > max_bytes:
            raise ResponseTooLargeError
        chain.append(current)
        location = _header(response.headers, "location")
        if response.status not in _REDIRECTS:
            return FetchedPage(url, tuple(chain), tuple((key.lower(), value) for key, value in response.headers), _body_text(response.body, response.headers))
        if location is None:
            raise ExchangeError("redirect without location")
        followed += 1
        if followed > MAX_REDIRECTS:
            raise ExchangeError(f"more than {MAX_REDIRECTS} redirects")
        current = _next_url(current, location)


def _fetch_one(url: str, dns: Dns, exchange: Exchange, timeout: Seconds, max_bytes: Bytes, user_agent: str, clock: Callable[[], float], deadline: float) -> FetchedPage | FetchError:
    try:
        return _fetch_checked(url, dns, exchange, timeout, max_bytes, user_agent, clock, deadline)
    except ResponseTooLargeError:
        return FetchError(url, f"response exceeds {format_size(max_bytes)}")
    except ExchangeError as error:
        return FetchError(url, str(error))
    except OSError as error:
        return FetchError(url, str(error) or "network error")


def fetch_pages(
    urls: Sequence[str], *, dns: Dns, exchange: Exchange, timeout: Seconds, max_bytes: Bytes, user_agent: str, clock: Callable[[], float] = time.monotonic
) -> tuple[FetchedPage | FetchError, ...]:
    if not urls:
        return ()
    deadline = clock() + OVERALL_SECONDS
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = [pool.submit(_fetch_one, url, dns, exchange, timeout, max_bytes, user_agent, clock, deadline) for url in urls]
        return tuple(future.result() for future in futures)
