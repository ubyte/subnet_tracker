from collections.abc import Collection, Sequence

from publicsuffixlist import PublicSuffixList

from subnet_tracker.discovery.parse import Rejected, normalize_name
from subnet_tracker.types import MAX_GROUP_NAME

_PSL = PublicSuffixList()
_SUFFIX_TRIES = 1000


def _normalized(host: str) -> str | None:
    name = normalize_name(host[2:] if host.startswith("*.") else host)
    return None if isinstance(name, Rejected) else name


def registrable_domain(host: str) -> str | None:
    normalized = _normalized(host)
    if normalized is None:
        return None
    suffix = _PSL.privatesuffix(normalized)
    return suffix if isinstance(suffix, str) else None


# registrable domain, or the domain itself when it is a public suffix and has none
def site_key(host: str) -> str | None:
    normalized = _normalized(host)
    if normalized is None:
        return None
    return registrable_domain(normalized) or normalized


def _fit(base: str, suffix: str) -> str:
    if len(suffix) >= MAX_GROUP_NAME:
        return suffix[:MAX_GROUP_NAME]
    return base[: MAX_GROUP_NAME - len(suffix)] + suffix


def scan_group(hosts: Sequence[str], taken: Collection[str]) -> str:
    sites: list[str] = []
    for host in hosts:
        site = registrable_domain(host)
        if site is not None and site not in sites:
            sites.append(site)
    if not sites:
        return ""
    extra = "" if len(sites) == 1 else f" +{len(sites) - 1}"
    base = _fit(sites[0], extra)
    if base not in taken:
        return base
    n = 2
    while n < _SUFFIX_TRIES:
        candidate = _fit(base, f" ({n})")
        if candidate not in taken:
            return candidate
        n += 1
    return base
