import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv6Network, ip_network

from subnet_tracker.config import IPNetwork
from subnet_tracker.discovery.parse import Rejected, normalize_name
from subnet_tracker.types import format_duration, format_size, parse_duration, parse_size

type Json = bool | int | float | str | list[str]
type Validate[T] = Callable[[T], None]

_INTEGER = re.compile(r"-?[0-9]+")
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_FLAGS = {"true": True, "false": False}
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]{1,512}")
_LIST_SEPARATOR = re.compile(r"[\s,]+")


@dataclass(frozen=True, slots=True)
class Codec[T]:
    """Converts between a value, its text form (UI, CLI, export) and its JSON form (settings.value); parse and decode raise ValueError."""

    parse: Callable[[str], T]
    format: Callable[[T], str]
    decode: Callable[[object], T]
    encode: Callable[[T], Json]


def _unexpected(kind: str, raw: object) -> ValueError:
    return ValueError(f"expected {kind}, got {json.dumps(raw)}")


def _from_string[T](parse: Callable[[str], T]) -> Callable[[object], T]:
    def decode(raw: object) -> T:
        if not isinstance(raw, str):
            raise _unexpected("a JSON string", raw)
        return parse(raw)

    return decode


def _string_codec[T](parse: Callable[[str], T], format: Callable[[T], str]) -> Codec[T]:
    return Codec(parse, format, _from_string(parse), format)


def _parse_integer(text: str) -> int:
    if not _INTEGER.fullmatch(text):
        raise ValueError(f"invalid integer {text!r}")
    return int(text)


def _decode_integer(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _unexpected("a JSON integer", raw)
    return raw


def _parse_decimal(text: str) -> float:
    if not _DECIMAL.fullmatch(text):
        raise ValueError(f"invalid number {text!r}, expected digits with an optional decimal point")
    return float(text)


def _decode_number(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise _unexpected("a JSON number", raw)
    return float(raw)


def _parse_flag(text: str) -> bool:
    if (flag := _FLAGS.get(text)) is None:
        raise ValueError(f"expected true or false, got {text!r}")
    return flag


def _decode_flag(raw: object) -> bool:
    if not isinstance(raw, bool):
        raise _unexpected("JSON true or false", raw)
    return raw


def _text(text: str) -> str:
    return text


def _network(text: str) -> IPNetwork:
    try:
        net = ip_network(text)
    except ValueError as e:
        raise ValueError(f"invalid network {text!r}: {e}") from None
    if isinstance(net, IPv6Network) and net.network_address.scope_id:
        raise ValueError(f"invalid network {text!r}: zone index is not routable")
    return net


def _domain(text: str) -> str:
    match normalize_name(text):
        case Rejected(reason):
            raise ValueError(f"invalid domain name {text!r}: {reason}")
        case name:
            return name


def _list_codec[T](parse: Callable[[str], T], format: Callable[[T], str]) -> Codec[tuple[T, ...]]:
    def unique(items: Iterable[str]) -> tuple[T, ...]:
        return tuple(dict.fromkeys(map(parse, items)))

    def decode(raw: object) -> tuple[T, ...]:
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise _unexpected("a JSON array of strings", raw)
        return unique(raw)

    return Codec(lambda text: unique(filter(None, _LIST_SEPARATOR.split(text))), lambda value: ", ".join(map(format, value)), decode, lambda value: list(map(format, value)))


def choice_codec[E: StrEnum](allowed: tuple[E, ...]) -> Codec[E]:
    by_value = {member.value: member for member in allowed}

    def parse(text: str) -> E:
        if (member := by_value.get(text)) is None:
            raise ValueError(f"expected one of {', '.join(by_value)}, got {text!r}")
        return member

    return _string_codec(parse, str)


DURATION = _string_codec(parse_duration, format_duration)
SIZE = _string_codec(parse_size, format_size)
INTEGER = Codec(_parse_integer, str, _decode_integer, int)
DECIMAL = Codec(_parse_decimal, str, _decode_number, float)
FLAG = Codec(_parse_flag, lambda flag: str(flag).lower(), _decode_flag, bool)
TEXT = _string_codec(_text, _text)
NETWORKS = _list_codec(_network, str)
DOMAINS = _list_codec(_domain, _text)


def accept(value: object) -> None:
    pass


def at_least[N: int](minimum: N, format: Callable[[N], str] = str) -> Validate[N]:
    def validate(value: N) -> None:
        if value < minimum:
            raise ValueError(f"must be at least {format(minimum)}")

    return validate


def within(limits: range) -> Validate[int]:
    def validate(value: int) -> None:
        if value not in limits:
            raise ValueError(f"must be between {limits[0]} and {limits[-1]}")

    return validate


def ratio(value: float) -> None:
    if not 0 < value <= 1:
        raise ValueError("must be greater than 0 and at most 1")


def printable_ascii(value: str) -> None:
    if not _PRINTABLE_ASCII.fullmatch(value):
        raise ValueError("expected 1 to 512 printable ASCII characters")
