from dataclasses import dataclass

from subnet_tracker.discovery.parse import Network
from subnet_tracker.types import DomainName, IPAddress, Timestamp


def network_order(net: Network) -> tuple[int, int, int]:
    return net.version, int(net.network_address), net.prefixlen


@dataclass(frozen=True, slots=True)
class SettingLine:
    line: int
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class GroupSection:
    line: int
    name: str
    description: str | None = None
    prefix_v4: int | None = None
    prefix_v6: int | None = None
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class DomainLine:
    line: int
    name: DomainName
    # the section group first, then @groups
    groups: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    prefix_v4: int | None = None
    prefix_v6: int | None = None
    enabled: bool = True
    comment: str = ""


@dataclass(frozen=True, slots=True)
class RouteLine:
    line: int
    network: Network
    groups: tuple[str, ...] = ()
    enabled: bool = True
    comment: str = ""


@dataclass(frozen=True, slots=True)
class DuplicateLine:
    line: int
    name: DomainName
    first_line: int


@dataclass(frozen=True, slots=True)
class ImportFile:
    settings: tuple[SettingLine, ...] = ()
    groups: tuple[GroupSection, ...] = ()
    domains: tuple[DomainLine, ...] = ()
    routes: tuple[RouteLine, ...] = ()
    duplicates: tuple[DuplicateLine, ...] = ()


@dataclass(frozen=True, slots=True)
class ExportSetting:
    key: str
    # the registry's text form, not the JSON stored in settings.value
    value: str


@dataclass(frozen=True, slots=True)
class ExportGroup:
    name: str
    description: str = ""
    prefix_v4: int | None = None
    prefix_v6: int | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ExportDomain:
    name: DomainName
    groups: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    prefix_v4: int | None = None
    prefix_v6: int | None = None
    enabled: bool = True
    comment: str = ""


@dataclass(frozen=True, slots=True)
class ExportRoute:
    network: Network
    groups: frozenset[str] = frozenset()
    enabled: bool = True
    comment: str = ""


@dataclass(frozen=True, slots=True)
class ExportAddress:
    name: DomainName
    ip: IPAddress
    resolver: str
    first_seen: Timestamp
    last_seen: Timestamp


@dataclass(frozen=True, slots=True)
class ExportFile:
    settings: tuple[ExportSetting, ...] = ()
    groups: tuple[ExportGroup, ...] = ()
    domains: tuple[ExportDomain, ...] = ()
    routes: tuple[ExportRoute, ...] = ()
    # None without --with-addresses
    addresses: tuple[ExportAddress, ...] | None = None
