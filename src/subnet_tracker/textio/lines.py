import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum, auto
from ipaddress import ip_network

from subnet_tracker.discovery.parse import Network, Rejected, normalize_name
from subnet_tracker.textio.model import DomainLine, GroupSection, RouteLine
from subnet_tracker.textio.tokens import QUOTED, Tokens, Word, tokens, unescape
from subnet_tracker.types import PREFIX_V4, PREFIX_V6, ParseError, ParseResult, zone_error

MAX_GROUP_NAME = 100
MAX_TAG_NAME = 50
DISABLED = "!disabled"
_HEADER = re.compile(rf"\[(settings|ungrouped|routes|addresses)\]|\[group\s+{QUOTED}\]")
_KEY_VALUE = re.compile(r"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)\s*=(.*)")
_PREFIX = re.compile(r"(v6)?/(.*)")
_DIGITS = re.compile(r"[0-9]+")
_BOOLEANS = {"true": True, "false": False}


class Section(StrEnum):
    SETTINGS = auto()
    UNGROUPED = auto()
    ROUTES = auto()
    ADDRESSES = auto()


@dataclass(frozen=True, slots=True)
class GroupHeader:
    name: str


@dataclass(frozen=True, slots=True)
class KeyValue:
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class _Attributes:
    groups: tuple[str, ...]
    tags: tuple[str, ...]
    prefix_v4: int | None
    prefix_v6: int | None
    enabled: bool


def _sized(name: str, limit: int, what: str) -> str | Rejected:
    return name if 1 <= len(name) <= limit else Rejected(f"{what} name must be 1 to {limit} characters long")


def _prefix(text: str, allowed: range) -> int | Rejected:
    if _DIGITS.fullmatch(text) and int(text) in allowed:
        return int(text)
    return Rejected(f"prefix length must be {allowed.start} to {allowed.stop - 1}")


def header(text: str) -> Section | GroupHeader | Rejected:
    if not (m := _HEADER.fullmatch(text)):
        return Rejected(f"unknown section header {text!r}")
    if m[1]:
        return Section(m[1])
    name = _sized(unescape(m[2]), MAX_GROUP_NAME, "group")
    return name if isinstance(name, Rejected) else GroupHeader(name)


def key_value(text: str) -> KeyValue | None:
    return KeyValue(m[1], m[2].strip()) if (m := _KEY_VALUE.fullmatch(text)) else None


def group_property(group: GroupSection, kv: KeyValue) -> GroupSection | Rejected:
    match kv.key:
        case "description":
            return replace(group, description=kv.value)
        case "prefix_v4":
            prefix = _prefix(kv.value, PREFIX_V4)
            return prefix if isinstance(prefix, Rejected) else replace(group, prefix_v4=prefix)
        case "prefix_v6":
            prefix = _prefix(kv.value, PREFIX_V6)
            return prefix if isinstance(prefix, Rejected) else replace(group, prefix_v6=prefix)
        case "enabled":
            enabled = _BOOLEANS.get(kv.value)
            return Rejected("enabled must be true or false") if enabled is None else replace(group, enabled=enabled)
    return Rejected(f"unknown group property {kv.key!r}")


def _attributes(words: Sequence[Word], groups: tuple[str, ...]) -> tuple[_Attributes, list[str]]:
    named: dict[str, dict[str, None]] = {"@": dict.fromkeys(groups), "#": {}}
    prefixes: dict[bool, int] = {}
    enabled = True
    errors: list[str] = []
    for word in words:
        match word:
            case Word("@" | "#" as sigil, name):
                limit, what = (MAX_GROUP_NAME, "group") if sigil == "@" else (MAX_TAG_NAME, "tag")
                match _sized(name, limit, what):
                    case Rejected(reason):
                        errors.append(f"{word.raw!r}: {reason}")
                    case _:
                        named[sigil][name] = None
            case Word("", text) if text == DISABLED:
                enabled = False
            case Word("", text) if m := _PREFIX.fullmatch(text):
                v6 = bool(m[1])
                match _prefix(m[2], PREFIX_V6 if v6 else PREFIX_V4):
                    case Rejected(reason):
                        errors.append(f"{word.raw!r}: {reason}")
                    case _ if v6 in prefixes:
                        errors.append(f"{word.raw!r}: prefix length is already given")
                    case prefix:
                        prefixes[v6] = prefix
            case _:
                errors.append(f"{word.raw!r}: unexpected attribute")
    return _Attributes(tuple(named["@"]), tuple(named["#"]), prefixes.get(False), prefixes.get(True), enabled), errors


def _line_result[T](line: int, value: T, errors: Sequence[str]) -> ParseResult[T | None]:
    return ParseResult(None, tuple(ParseError(line, e) for e in errors)) if errors else ParseResult(value)


def _split(text: str, what: str) -> tuple[Word, Tokens] | Rejected:
    match tokens(text):
        case Tokens((Word("", _) as head, *_)) as parsed:
            return head, parsed
        case Rejected() as rejected:
            return rejected
    return Rejected(f"line must start with {what}")


def domain_line(line: int, text: str, group: str | None) -> ParseResult[DomainLine | None]:
    split = _split(text, "a domain name")
    if isinstance(split, Rejected):
        return _line_result(line, None, [split.reason])
    head, parsed = split
    attrs, errors = _attributes(parsed.words[1:], () if group is None else (group,))
    match normalize_name(head.text):
        case Rejected(reason):
            return _line_result(line, None, [f"{head.raw!r}: {reason}", *errors])
        case name:
            return _line_result(line, DomainLine(line, name, attrs.groups, attrs.tags, attrs.prefix_v4, attrs.prefix_v6, attrs.enabled, parsed.comment), errors)


def _network(text: str) -> Network | Rejected:
    try:
        net = ip_network(text, strict=False)
    except ValueError:
        return Rejected("not an address or CIDR")
    return net if (error := zone_error(net)) is None else Rejected(error)


def route_line(line: int, text: str) -> ParseResult[RouteLine | None]:
    split = _split(text, "an address or CIDR")
    if isinstance(split, Rejected):
        return _line_result(line, None, [split.reason])
    head, parsed = split
    allowed = [w for w in parsed.words[1:] if w.sigil == "@" or w.raw == DISABLED]
    attrs, errors = _attributes(allowed, ())
    errors += [f"{w.raw!r}: only @group and {DISABLED} are allowed in [routes]" for w in parsed.words[1:] if w not in allowed]
    match _network(head.text):
        case Rejected(reason):
            return _line_result(line, None, [f"{head.raw!r}: {reason}", *errors])
        case net:
            return _line_result(line, RouteLine(line, net, attrs.groups, attrs.enabled, parsed.comment), errors)
