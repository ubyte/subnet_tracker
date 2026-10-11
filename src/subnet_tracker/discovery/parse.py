import re
from collections.abc import Container, Iterator
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from urllib.parse import urlsplit

import idna

from subnet_tracker.discovery.www import DomainNames, domain_names, pair_name
from subnet_tracker.types import DomainName, ParseError, ParseResult

type Network = IPv4Network | IPv6Network

MAX_NAME_LENGTH = 253
WILDCARD = "*."
_SEPARATORS = re.compile(r"[\s,]+")
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: str


@dataclass(frozen=True, slots=True)
class PastePlan:
    new_domains: tuple[DomainNames, ...] = ()
    duplicates: tuple[DomainName, ...] = ()
    networks: tuple[Network, ...] = ()
    wildcards: tuple[DomainName, ...] = ()


@dataclass(frozen=True, slots=True)
class _Host:
    name: DomainName
    wildcard: bool


def normalize_name(host: str) -> DomainName | Rejected:
    try:
        name = DomainName(idna.encode(host, uts46=True).decode("ascii").removesuffix("."))
    except idna.IDNAError as e:
        return Rejected(str(e))
    labels = name.split(".")
    if len(labels) < 2:
        return Rejected("a single label is not a domain name")
    if labels[-1].isdigit():
        return Rejected("numeric top-level label")
    if len(pair_name(name)) > MAX_NAME_LENGTH:
        return Rejected(f"its www pair would be longer than {MAX_NAME_LENGTH} characters")
    return name


def _network(text: str) -> Network | None:
    try:
        return ip_network(text, strict=False)
    except ValueError:
        return None


def _routable(net: Network) -> Network | Rejected:
    return Rejected("zone index is not routable") if isinstance(net, IPv6Network) and net.network_address.scope_id else net


def _hostname(token: str) -> str | None:
    try:
        return urlsplit(token if _SCHEME.match(token) else "//" + token.removeprefix("//")).hostname
    except ValueError:
        return None


def _classify(token: str) -> _Host | Network | Rejected:
    if (net := _network(token)) is not None:
        return _routable(net)
    address, _, prefix = token.partition("/")
    if prefix.isdigit() and _network(address) is not None:
        return Rejected("invalid prefix length")
    if not (host := _hostname(token)):
        return Rejected("no host")
    if (net := _network(host)) is not None:
        return _routable(net)
    name = normalize_name(host.removeprefix(WILDCARD))
    return name if isinstance(name, Rejected) else _Host(name, host.startswith(WILDCARD))


def _tokens(text: str) -> Iterator[tuple[int, str]]:
    for line, content in enumerate(text.splitlines(), 1):
        yield from ((line, token) for token in _SEPARATORS.split(content) if token)


def parse_paste(text: str, existing: Container[DomainName]) -> ParseResult[PastePlan]:
    domains: list[DomainNames] = []
    duplicates: list[DomainName] = []
    networks: dict[Network, None] = {}
    wildcards: dict[DomainName, None] = {}
    errors: list[ParseError] = []
    taken: set[DomainName] = set()
    for line, token in _tokens(text):
        match _classify(token):
            case Rejected(reason):
                errors.append(ParseError(line, f"{token!r}: {reason}"))
            case _Host(name, wildcard):
                if wildcard:
                    wildcards[name] = None
                names = domain_names(name)
                if any(n in existing or n in taken for n in names.all):
                    duplicates.append(name)
                else:
                    domains.append(names)
                    taken.update(names.all)
            case net:
                networks[net] = None
    return ParseResult(PastePlan(tuple(domains), tuple(duplicates), tuple(networks), tuple(wildcards)), tuple(errors))
