from dataclasses import dataclass

WWW = "www."


@dataclass(frozen=True, slots=True)
class DomainNames:
    canonical: str
    pair: str

    @property
    def all(self) -> tuple[str, str]:
        return (self.canonical, self.pair)


def pair_name(name: str) -> str:
    return name.removeprefix(WWW) if name.startswith(WWW) else WWW + name


def domain_names(canonical: str) -> DomainNames:
    return DomainNames(canonical, pair_name(canonical))
