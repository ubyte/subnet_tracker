import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import NewType

type IPAddress = IPv4Address | IPv6Address

Timestamp = NewType("Timestamp", int)
Seconds = NewType("Seconds", int)

GroupId = NewType("GroupId", int)
BatchId = NewType("BatchId", int)
DomainId = NewType("DomainId", int)
NameId = NewType("NameId", int)
TagId = NewType("TagId", int)
ResolverId = NewType("ResolverId", int)
AgentId = NewType("AgentId", int)
AddressId = NewType("AddressId", int)
GlobalRouteId = NewType("GlobalRouteId", int)
PlanRouteId = NewType("PlanRouteId", int)
WebDeviceId = NewType("WebDeviceId", int)
RequestId = NewType("RequestId", int)
ResolveRunId = NewType("ResolveRunId", int)

# lowercase punycode, without the trailing dot
DomainName = NewType("DomainName", str)


@dataclass(frozen=True, slots=True)
class ParseError:
    line: int
    message: str


@dataclass(frozen=True, slots=True)
class ParseResult[T]:
    value: T
    errors: tuple[ParseError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


_DURATION = re.compile(r"([0-9]+)([smhd])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> Seconds:
    if not (m := _DURATION.fullmatch(text)):
        raise ValueError(f"invalid duration {text!r}, expected <number><s|m|h|d>")
    return Seconds(int(m[1]) * _UNIT_SECONDS[m[2]])


@dataclass(frozen=True, slots=True)
class IpRange:
    first: IPAddress
    last: IPAddress

    def __post_init__(self) -> None:
        if self.first.version != self.last.version:
            raise ValueError("ends of the range are of different families")
        if int(self.first) > int(self.last):
            raise ValueError("first address of the range is after the last")

    def __contains__(self, ip: IPAddress) -> bool:
        return ip.version == self.first.version and int(self.first) <= int(ip) <= int(self.last)


def _ip_range(text: str) -> IpRange:
    match text.split("-"):
        case [one]:
            net = ip_network(one)
            return IpRange(net.network_address, net.broadcast_address)
        case [first, last]:
            return IpRange(ip_address(first), ip_address(last))
    raise ValueError("expected CIDR, address or first-last")


def parse_ip_range(text: str) -> IpRange:
    try:
        return _ip_range(text)
    except ValueError as e:
        raise ValueError(f"invalid address range {text!r}: {e}") from None
