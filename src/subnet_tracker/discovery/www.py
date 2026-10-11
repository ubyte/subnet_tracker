from collections.abc import Container, Iterable
from dataclasses import dataclass

from subnet_tracker.types import DomainName

WWW = "www."


@dataclass(frozen=True, slots=True)
class DomainNames:
    canonical: DomainName
    pair: DomainName

    @property
    def all(self) -> tuple[DomainName, DomainName]:
        return (self.canonical, self.pair)


@dataclass(frozen=True, slots=True)
class Deduplicated:
    new_domains: tuple[DomainNames, ...] = ()
    duplicates: tuple[DomainName, ...] = ()


def pair_name(name: DomainName) -> DomainName:
    return DomainName(name.removeprefix(WWW) if name.startswith(WWW) else WWW + name)


def domain_names(canonical: DomainName) -> DomainNames:
    return DomainNames(canonical, pair_name(canonical))


def deduplicate(entered: Iterable[DomainName], existing: Container[DomainName]) -> Deduplicated:
    domains: list[DomainNames] = []
    duplicates: list[DomainName] = []
    taken: set[DomainName] = set()
    for name in entered:
        names = domain_names(name)
        if any(n in existing or n in taken for n in names.all):
            duplicates.append(name)
        else:
            domains.append(names)
            taken.update(names.all)
    return Deduplicated(tuple(domains), tuple(duplicates))
