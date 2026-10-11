import re
from collections.abc import Collection, Container, Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from html.parser import HTMLParser
from urllib.parse import urlsplit

from subnet_tracker.discovery.parse import Rejected, normalize_name
from subnet_tracker.discovery.scan_group import site_key
from subnet_tracker.discovery.www import pair_name
from subnet_tracker.types import DomainName

_URL_ATTRS = frozenset({"src", "href", "srcset", "action", "poster", "data-src"})
_REFRESH = re.compile(r"url\s*=\s*['\"]?([^'\"\s;]+)", re.IGNORECASE)
_STYLE_URL = re.compile(r"url\(\s*['\"]?([^)'\"\s]+)", re.IGNORECASE)
_SCRIPT_HOST = re.compile(r"https://[^\s\"'`<>\\)#?/]+|(?<!:)//[^\s\"'`<>\\)#?/]+", re.IGNORECASE)


class ScanRole(StrEnum):
    SAME = auto()
    THIRD = auto()


@dataclass(frozen=True, slots=True)
class Occurrence:
    name: DomainName
    source: str


@dataclass(frozen=True, slots=True)
class Candidate:
    name: DomainName
    role: ScanRole
    exists: bool
    sources: tuple[str, ...]
    registrable: str

    @property
    def selected(self) -> bool:
        return self.role == ScanRole.SAME and not self.exists


def url_host(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return parts.hostname


def _domain(host: str) -> DomainName | None:
    name = normalize_name(host.removeprefix("*."))
    return None if isinstance(name, Rejected) else name


def _host(value: str) -> str | None:
    text = value.strip()
    if not text or text.lower().startswith(("data:", "javascript:", "mailto:", "blob:", "about:")):
        return None
    if "://" not in text and not text.startswith("//"):
        return None
    try:
        return urlsplit(text).hostname
    except ValueError:
        return None


def _csp_hosts(value: str) -> list[str]:
    hosts: list[str] = []
    for raw in value.split():
        token = raw.strip().rstrip(";")
        if not token or token.startswith("'") or (token.endswith(":") and "://" not in token):
            continue
        if token.startswith("*."):
            hosts.append(token[2:].split("/")[0])
            continue
        if "://" in token or token.startswith("//"):
            host = _host(token)
            if host is not None:
                hosts.append(host.removeprefix("*."))
            continue
        bare = token.split("/")[0]
        if "." in bare and (host := _host("//" + bare)) is not None:
            hosts.append(host)
    return hosts


def _srcset(value: str) -> list[str]:
    urls: list[str] = []
    for part in value.split(","):
        token = part.strip().split()
        if token:
            urls.append(token[0])
    return urls


def _label(tag: str, key: str, rels: set[str]) -> str:
    if tag == "link" and key == "href":
        if "preconnect" in rels:
            return "preconnect"
        if "dns-prefetch" in rels:
            return "dns-prefetch"
        return "link href"
    return f"{tag} {key}"


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[Occurrence] = []
        self._mode: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        rels: set[str] = set()
        refresh = False
        for key, value in attrs:
            if key == "rel" and value:
                rels.update(part.lower() for part in value.split())
            if key == "http-equiv" and value and value.lower() == "refresh":
                refresh = True
        for key, value in attrs:
            if not value:
                continue
            if key == "style":
                for url in _STYLE_URL.findall(value):
                    self._add(url, "style")
            if tag == "meta" and key == "content" and refresh and (match := _REFRESH.search(value)):
                self._add(match.group(1), "meta refresh")
            if key not in _URL_ATTRS:
                continue
            label = _label(tag, key, rels)
            for url in _srcset(value) if key == "srcset" else (value,):
                self._add(url, label)
        if tag in {"script", "style"}:
            self._flush()
            self._mode = tag
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == self._mode:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._mode is not None:
            self._parts.append(data)

    def close(self) -> None:
        self._flush()
        super().close()

    def _add(self, url: str, source: str) -> None:
        host = _host(url)
        if host is not None:
            self._add_host(host, source)

    def _add_host(self, host: str, source: str) -> None:
        name = _domain(host)
        if name is not None:
            self.found.append(Occurrence(name, source))

    def _flush(self) -> None:
        if self._mode is None:
            return
        text = "".join(self._parts)
        if self._mode == "style":
            for url in _STYLE_URL.findall(text):
                self._add(url, "style")
        else:
            for match in _SCRIPT_HOST.finditer(text.replace("\\/", "/")):
                host = _host(match.group(0))
                if host is not None:
                    self._add_host(host, "script")
        self._mode = None
        self._parts = []


def extract(chain: Sequence[str], headers: Sequence[tuple[str, str]], body: str) -> tuple[Occurrence, ...]:
    found: list[Occurrence] = []
    for index, url in enumerate(chain):
        host = url_host(url)
        if host is not None and (name := _domain(host)) is not None:
            found.append(Occurrence(name, "page host" if index == 0 else "redirect"))
    for key, value in headers:
        if key.lower() == "content-security-policy":
            for host in _csp_hosts(value):
                if (name := _domain(host)) is not None:
                    found.append(Occurrence(name, "CSP"))
    parser = _Parser()
    parser.feed(body)
    parser.close()
    found.extend(parser.found)
    return tuple(found)


def _ignored(name: DomainName, ignore: Sequence[str]) -> str | None:
    pair = pair_name(name)
    for entry in ignore:
        if name == entry or name.endswith("." + entry) or pair == entry or pair.endswith("." + entry):
            return entry
    return None


def classify(found: Sequence[Occurrence], sites: Collection[str], existing: Container[DomainName], ignore: Sequence[str]) -> tuple[tuple[Candidate, ...], tuple[str, ...]]:
    # Same site and third-party groups use the registrable domain (9.2). A www pair is one domain by the textual rule (9.4), not by the PSL.
    canonical_of: dict[DomainName, DomainName] = {}
    order: list[DomainName] = []
    sources: dict[DomainName, list[str]] = {}
    for item in found:
        canonical = canonical_of.get(item.name)
        if canonical is None:
            canonical = canonical_of.get(pair_name(item.name))
        if canonical is None:
            canonical = item.name
            order.append(canonical)
            sources[canonical] = []
            canonical_of[canonical] = canonical
            canonical_of[pair_name(canonical)] = canonical
        if item.source not in sources[canonical]:
            sources[canonical].append(item.source)
    ignored: list[str] = []
    built: list[Candidate] = []
    for name in order:
        entry = _ignored(name, ignore)
        if entry is not None:
            if entry not in ignored:
                ignored.append(entry)
            continue
        key = site_key(name) or name
        pair_key = site_key(pair_name(name))
        role = ScanRole.SAME if key in sites or (pair_key is not None and pair_key in sites) else ScanRole.THIRD
        built.append(Candidate(name, role, name in existing or pair_name(name) in existing, tuple(sources[name]), key))
    return _ordered(built), tuple(ignored)


def _ordered(built: Sequence[Candidate]) -> tuple[Candidate, ...]:
    same = [item for item in built if item.role == ScanRole.SAME]
    groups: dict[str, list[Candidate]] = {}
    for item in built:
        if item.role == ScanRole.THIRD:
            groups.setdefault(item.registrable, []).append(item)
    third = [item for bucket in groups.values() for item in bucket]
    return (*same, *third)
