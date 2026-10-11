from dataclasses import dataclass
from typing import cast

from subnet_tracker.discovery.parse import Rejected, normalize_name
from subnet_tracker.resolver.doh_json import classify_doh
from subnet_tracker.resolver.model import NameResult, QueryType, combine
from subnet_tracker.types import DomainName, JsonValue, ResolverId

_MAX_ELEMENTS = 500


@dataclass(frozen=True, slots=True)
class ElementRejection:
    index: int
    reason: str


@dataclass(frozen=True, slots=True)
class BrowserPair:
    name: DomainName
    resolver_id: ResolverId
    result: NameResult
    # index of the A element, then the AAAA element
    indices: tuple[int, int]


@dataclass(frozen=True, slots=True)
class BrowserReport:
    pairs: tuple[BrowserPair, ...]
    rejected: tuple[ElementRejection, ...]


@dataclass(frozen=True, slots=True)
class BrowserReportError:
    reason: str


@dataclass(frozen=True, slots=True)
class _Query:
    name: DomainName
    resolver_id: ResolverId
    qtype: QueryType
    http_status: int | None
    error: str | None
    body: JsonValue


def canonical_name(text: object) -> DomainName | None:
    if not isinstance(text, str):
        return None
    normalized = normalize_name(text)
    if isinstance(normalized, Rejected) or normalized != text:
        return None
    return normalized


def _mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    copied: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        copied[key] = item
    return copied


def _element(value: object) -> _Query | str:
    item = _mapping(value)
    if item is None:
        return "element is not an object"
    name = canonical_name(item.get("name"))
    if name is None:
        return "name is not in canonical form"
    qtype = item.get("type")
    if not isinstance(qtype, str) or qtype not in (QueryType.A, QueryType.AAAA):
        return "type is not A or AAAA"
    resolver_id = item.get("resolver_id")
    if type(resolver_id) is not int or resolver_id < 1:
        return "resolver_id is not a positive integer"
    http_status = item.get("http_status")
    if "http_status" in item and http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        return "http_status is outside 100..599"
    error = item.get("error")
    if "error" in item and error is not None and not isinstance(error, str):
        return "error is not a string"
    usable_error = isinstance(error, str) and bool(" ".join(error.split()))
    if type(http_status) is not int and not usable_error:
        return "neither http_status nor error"
    http = http_status if type(http_status) is int else None
    return _Query(name, ResolverId(resolver_id), QueryType(qtype), http, error if isinstance(error, str) else None, cast(JsonValue, item.get("body")))


def _pairs(valid: list[tuple[int, _Query]]) -> tuple[list[BrowserPair], list[ElementRejection]]:
    groups: dict[tuple[DomainName, ResolverId], list[tuple[int, _Query]]] = {}
    for index, query in valid:
        groups.setdefault((query.name, query.resolver_id), []).append((index, query))
    pairs: list[BrowserPair] = []
    rejected: list[ElementRejection] = []
    for members in groups.values():
        by_type: dict[QueryType, list[tuple[int, _Query]]] = {QueryType.A: [], QueryType.AAAA: []}
        for index, query in members:
            by_type[query.qtype].append((index, query))
        found_a, found_aaaa = by_type[QueryType.A], by_type[QueryType.AAAA]
        if len(found_a) == 1 and len(found_aaaa) == 1:
            a_index, a_query = found_a[0]
            aaaa_index, aaaa_query = found_aaaa[0]
            result = combine(
                classify_doh(a_query.name, QueryType.A, a_query.http_status, a_query.error, a_query.body),
                classify_doh(aaaa_query.name, QueryType.AAAA, aaaa_query.http_status, aaaa_query.error, aaaa_query.body),
            )
            pairs.append(BrowserPair(a_query.name, a_query.resolver_id, result, (a_index, aaaa_index)))
            continue
        reason = "repeated type" if len(found_a) > 1 or len(found_aaaa) > 1 else "missing the other record type"
        rejected.extend(ElementRejection(index, reason) for index, _query in members)
    pairs.sort(key=lambda pair: min(pair.indices))
    return pairs, rejected


def parse_browser_report(payload: object) -> BrowserReport | BrowserReportError:
    if not isinstance(payload, list):
        return BrowserReportError("not a JSON array")
    if len(payload) > _MAX_ELEMENTS:
        return BrowserReportError("more than 500 elements")
    rejected: list[ElementRejection] = []
    valid: list[tuple[int, _Query]] = []
    for index, value in enumerate(payload):
        parsed = _element(value)
        if isinstance(parsed, str):
            rejected.append(ElementRejection(index, parsed))
        else:
            valid.append((index, parsed))
    pairs, unpaired = _pairs(valid)
    rejected.extend(unpaired)
    rejected.sort(key=lambda item: item.index)
    return BrowserReport(tuple(pairs), tuple(rejected))
