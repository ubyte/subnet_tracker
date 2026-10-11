from dataclasses import dataclass
from enum import StrEnum
from functools import cache

from publicsuffixlist import PublicSuffixList

WWW = "www."
MAX_NAME_LENGTH = 253


class WwwPair(StrEnum):
    ALL = "all"
    REGISTRABLE = "registrable"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class DomainNames:
    canonical: str
    pair: str | None

    @property
    def all(self) -> tuple[str, ...]:
        return (self.canonical,) if self.pair is None else (self.canonical, self.pair)


@cache
def _psl() -> PublicSuffixList:
    return PublicSuffixList()


def _toggle_www(name: str) -> str:
    return name.removeprefix(WWW) if name.startswith(WWW) else WWW + name


def _registrable(name: str) -> bool:
    return _psl().privatesuffix(name) == name


def pair_name(name: str, mode: WwwPair) -> str | None:
    if mode is WwwPair.OFF:
        return None
    pair = _toggle_www(name)
    if len(pair) > MAX_NAME_LENGTH or _psl().is_public(name) or _psl().is_public(pair):
        return None
    if mode is WwwPair.REGISTRABLE and not (_registrable(name) or _registrable(pair)):
        return None
    return pair


def domain_names(canonical: str, www_pair: WwwPair) -> DomainNames:
    return DomainNames(canonical, pair_name(canonical, www_pair))
