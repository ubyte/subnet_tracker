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


def pair_name(name: DomainName) -> DomainName:
    return DomainName(name.removeprefix(WWW) if name.startswith(WWW) else WWW + name)


def domain_names(canonical: DomainName) -> DomainNames:
    return DomainNames(canonical, pair_name(canonical))
