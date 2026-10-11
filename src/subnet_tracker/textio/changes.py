from dataclasses import dataclass, field
from enum import StrEnum, auto

from subnet_tracker.core.settings import SettingValue
from subnet_tracker.textio.model import DuplicateLine, ExportDomain, ExportGroup, ExportRoute


class ImportMode(StrEnum):
    MERGE = auto()
    REPLACE = auto()


@dataclass(frozen=True, slots=True)
class Update[T]:
    old: T
    new: T


@dataclass(frozen=True, slots=True)
class Counts:
    added: int = 0
    updated: int = 0
    removed: int = 0


@dataclass(frozen=True, slots=True)
class Changes[T]:
    added: tuple[T, ...] = ()
    updated: tuple[Update[T], ...] = ()
    removed: tuple[T, ...] = ()

    @property
    def counts(self) -> Counts:
        return Counts(len(self.added), len(self.updated), len(self.removed))


@dataclass(frozen=True, slots=True)
class SettingChange:
    key: str
    # the stored text form, None while the setting has its default
    old: str | None
    # a default value resets the setting: defaults are never stored
    new: SettingValue


@dataclass(frozen=True, slots=True)
class ImportDiff:
    settings: tuple[SettingChange, ...] = ()
    groups: Changes[ExportGroup] = field(default_factory=Changes)
    # an update with another name keeps both names of the domain and flips names.canonical
    domains: Changes[ExportDomain] = field(default_factory=Changes)
    routes: Changes[ExportRoute] = field(default_factory=Changes)
    duplicates: tuple[DuplicateLine, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportSummary:
    settings: Counts
    groups: Counts
    domains: Counts
    # domain updates that flip the canonical name
    canonical: int
    routes: Counts
    duplicates: int


def _setting_counts(changes: tuple[SettingChange, ...]) -> Counts:
    reset = sum(c.new.is_default for c in changes)
    added = sum(c.old is None for c in changes)
    return Counts(added, len(changes) - added - reset, reset)


def import_summary(diff: ImportDiff) -> ImportSummary:
    canonical = sum(u.old.name != u.new.name for u in diff.domains.updated)
    return ImportSummary(_setting_counts(diff.settings), diff.groups.counts, diff.domains.counts, canonical, diff.routes.counts, len(diff.duplicates))
