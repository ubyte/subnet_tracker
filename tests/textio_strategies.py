from ipaddress import ip_network

from hypothesis import strategies as st

from settings_samples import SAMPLES
from subnet_tracker.discovery.parse import Network
from subnet_tracker.discovery.www import domain_names, pair_name
from subnet_tracker.textio.model import ExportAddress, ExportDomain, ExportFile, ExportGroup, ExportRoute, ExportSetting
from subnet_tracker.types import MAX_GROUP_NAME, MAX_TAG_NAME, PREFIX_V4, PREFIX_V6, DomainName, FieldError, Timestamp, route_network

# everything str.splitlines() breaks on: names, descriptions and comments never contain these (design section 12)
LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_char = st.one_of(st.sampled_from(' \t\u00a0\u3000\x1f"\\;#@=![]'), st.characters(exclude_categories=["Cs"], exclude_characters=LINE_BREAKS))


def one_line_strings(min_size: int = 0, max_size: int = 30) -> st.SearchStrategy[str]:
    return st.text(_char, min_size=min_size, max_size=max_size)


def trimmed_text() -> st.SearchStrategy[str]:
    return one_line_strings().map(str.strip)


def _optional(allowed: range) -> st.SearchStrategy[int | None]:
    return st.none() | st.integers(allowed.start, allowed.stop - 1)


def _subsets(names: list[str]) -> st.SearchStrategy[frozenset[str]]:
    return st.frozensets(st.sampled_from(names), max_size=3) if names else st.just(frozenset())


def _distinct_pairs(domains: list[ExportDomain]) -> tuple[ExportDomain, ...]:
    seen: set[DomainName] = set()
    kept = []
    for d in domains:
        names = domain_names(d.name).all
        if seen.isdisjoint(names):
            kept.append(d)
            seen.update(names)
    return tuple(kept)


def _network(address: st.SearchStrategy[str], bits: int) -> st.SearchStrategy[Network]:
    return st.builds(lambda a, p: ip_network(f"{a}/{p}", strict=False), address, st.integers(0, bits))


_labels = st.lists(st.from_regex(r"[a-z0-9]{1,8}", fullmatch=True), min_size=1, max_size=3)
_domain_name = st.builds(lambda www, labels, tld: DomainName(www + ".".join([*labels, tld])), st.sampled_from(["", "www.", "www.www."]), _labels, st.from_regex(r"[a-z]{2,6}", fullmatch=True))
_timestamps = st.integers(0, 2**33).map(Timestamp)
_networks = (_network(st.ip_addresses(v=4).map(str), 32) | _network(st.ip_addresses(v=6).map(str), 128)).filter(lambda n: not isinstance(route_network("cidr", str(n)), FieldError))
_settings = st.lists(st.builds(ExportSetting, st.from_regex(r"[a-z][a-z0-9_]{0,8}(\.[a-z0-9_]{1,8}){0,2}", fullmatch=True), trimmed_text()), max_size=3, unique_by=lambda s: s.key).map(tuple)
_group_name = one_line_strings(1, MAX_GROUP_NAME)
_tags = st.frozensets(one_line_strings(1, MAX_TAG_NAME), max_size=3)
_address = st.builds(ExportAddress, _domain_name, st.ip_addresses(), one_line_strings(1, 100), _timestamps, _timestamps)
# a sighting is unique per name, IP and resolver: UNIQUE (name_id, ip) in addresses, PRIMARY KEY (address_id, resolver_id) in sightings
_addresses = st.none() | st.lists(_address, max_size=3, unique_by=lambda a: (a.name, a.ip, a.resolver)).map(tuple)
# stored settings: registry text form, never the default; two values for some keys so that states disagree
_STORED_VALUES = [*((key, canonical) for key, (_, canonical) in SAMPLES.items()), ("resolve.interval", "2h"), ("routes.max_v4", "700"), ("browser.refresh_mode", "off")]
_stored_settings = st.lists(st.sampled_from(_STORED_VALUES), max_size=4, unique_by=lambda kv: kv[0]).map(lambda kvs: tuple(ExportSetting(k, v) for k, v in kvs))


def _group(name: str) -> st.SearchStrategy[ExportGroup]:
    return st.builds(ExportGroup, st.just(name), trimmed_text(), _optional(PREFIX_V4), _optional(PREFIX_V6), st.booleans())


def _domain(name: DomainName, groups: list[str]) -> st.SearchStrategy[ExportDomain]:
    return st.builds(ExportDomain, st.just(name), _subsets(groups), _tags, _optional(PREFIX_V4), _optional(PREFIX_V6), st.booleans(), trimmed_text())


def _route(network: Network, groups: list[str]) -> st.SearchStrategy[ExportRoute]:
    return st.builds(ExportRoute, st.just(network), _subsets(groups), st.booleans(), trimmed_text())


@st.composite
def models(draw: st.DrawFn, settings: st.SearchStrategy[tuple[ExportSetting, ...]] = _settings, addresses: st.SearchStrategy[tuple[ExportAddress, ...] | None] = _addresses) -> ExportFile:
    groups = draw(st.lists(_group_name.flatmap(_group), max_size=4, unique_by=lambda g: g.name))
    names = [g.name for g in groups]
    domains = draw(st.lists(_domain_name.flatmap(lambda n: _domain(n, names)), max_size=8).map(_distinct_pairs))
    routes = draw(st.lists(_networks.flatmap(lambda n: _route(n, names)), max_size=4, unique_by=lambda r: r.network))
    return ExportFile(draw(settings), tuple(groups), domains, tuple(routes), draw(addresses))


def stored_states() -> st.SearchStrategy[ExportFile]:
    # what the CLI reads from the DB: registry settings without defaults, every referenced group exists, no addresses
    return models(_stored_settings, st.none())


@st.composite
def related_states(draw: st.DrawFn, base: ExportFile) -> ExportFile:
    # another stored state that reuses some groups, domain names (also as the pair or with one more www.) and networks of base with other attributes
    other = draw(stored_states())
    reused = [g.name for g in base.groups if draw(st.booleans())]
    groups = [*(draw(_group(name)) for name in reused), *(g for g in other.groups if g.name not in reused)]
    names = [g.name for g in groups]
    renamed = [draw(st.sampled_from([d.name, pair_name(d.name), DomainName("www." + d.name)])) for d in base.domains if draw(st.booleans())]
    other_domains = [draw(_domain(d.name, names)) for d in other.domains]
    domains = _distinct_pairs([*(draw(_domain(name, names)) for name in renamed), *other_domains])
    networks = [r.network for r in base.routes if draw(st.booleans())]
    routes = [*(draw(_route(n, names)) for n in networks), *(draw(_route(r.network, names)) for r in other.routes if r.network not in networks)]
    return ExportFile(other.settings, tuple(groups), domains, tuple(routes))
