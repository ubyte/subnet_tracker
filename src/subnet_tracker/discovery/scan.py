from collections.abc import Callable, Collection, Container, Sequence
from dataclasses import dataclass
from time import monotonic

from subnet_tracker.core.settings import DiscoverySettings
from subnet_tracker.discovery.extract import Candidate, Occurrence, classify, extract, url_host
from subnet_tracker.discovery.fetch import MAX_URLS, Dns, Exchange, FetchError, fetch_pages
from subnet_tracker.discovery.scan_group import scan_group, site_key
from subnet_tracker.types import DomainName


@dataclass(frozen=True, slots=True)
class ScanError:
    url: str
    message: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    candidates: tuple[Candidate, ...] = ()
    ignored: tuple[str, ...] = ()
    errors: tuple[ScanError, ...] = ()
    group_name: str = ""


def _hosts(urls: Sequence[str]) -> list[str]:
    hosts: list[str] = []
    for url in urls:
        host = url_host(url)
        if host is not None:
            hosts.append(host)
    return hosts


def _sites(urls: Sequence[str]) -> frozenset[str]:
    sites: list[str] = []
    for host in _hosts(urls):
        site = site_key(host)
        if site is not None and site not in sites:
            sites.append(site)
    return frozenset(sites)


def scan(
    urls: Sequence[str],
    *,
    existing: Container[DomainName],
    groups: Collection[str],
    settings: DiscoverySettings,
    dns: Dns,
    exchange: Exchange,
    clock: Callable[[], float] = monotonic,
) -> ScanResult:
    group_name = scan_group(_hosts(urls), groups)
    if len(urls) > MAX_URLS:
        return ScanResult((), (), (ScanError("", f"at most {MAX_URLS} URLs"),), group_name)
    fetched = fetch_pages(urls, dns=dns, exchange=exchange, timeout=settings.timeout, max_bytes=settings.max_bytes, user_agent=settings.user_agent, clock=clock)
    errors: list[ScanError] = []
    found: list[Occurrence] = []
    for item in fetched:
        if isinstance(item, FetchError):
            errors.append(ScanError(item.url, item.message))
            continue
        found.extend(extract(item.chain, item.headers, item.body))
    candidates, ignored = classify(tuple(found), _sites(urls), existing, settings.ignore)
    return ScanResult(candidates, ignored, tuple(errors), group_name)
