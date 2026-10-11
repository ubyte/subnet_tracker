from dataclasses import dataclass

from subnet_tracker.discovery.parse import Network
from subnet_tracker.types import DomainName


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
