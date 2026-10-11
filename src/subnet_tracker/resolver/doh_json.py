import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from ipaddress import IPv6Address, ip_address

from subnet_tracker.resolver.model import QueryResult, QueryType
from subnet_tracker.types import DomainName, IPAddress, JsonValue, ResolveStatus, Seconds

MAX_ADDRESSES = 64
MAX_CHAIN = 16
MAX_RECORDS = 256
_MAX_NAME = 253
_MAX_TTL = 2**31 - 1
_MAX_RCODE = 4095
_MAX_ERROR = 200
_CNAME = 5
_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
# IANA RCODE mnemonics; a code without a name is reported as its number
_RCODE_NAME = {
    1: "FORMERR",
    2: "SERVFAIL",
    4: "NOTIMP",
    5: "REFUSED",
    6: "YXDOMAIN",
    7: "YXRRSET",
    8: "NXRRSET",
    9: "NOTAUTH",
    10: "NOTZONE",
    11: "DSOTYPENI",
    16: "BADVERS",
    17: "BADKEY",
    18: "BADTIME",
    19: "BADMODE",
    20: "BADNAME",
    21: "BADALG",
    22: "BADTRUNC",
    23: "BADCOOKIE",
}


class DohProblem(StrEnum):
    MALFORMED = auto()
    QUESTION = auto()
    ADDRESS = auto()
    LIMIT = auto()
    CHAIN = auto()


@dataclass(frozen=True, slots=True)
class DohError:
    problem: DohProblem
    detail: str

    def __str__(self) -> str:
        return f"{self.problem}: {self.detail}"


@dataclass(frozen=True, slots=True)
class _Record:
    owner: DomainName | None
    rdtype: int
    data: str
    ttl: int


@dataclass(frozen=True, slots=True)
class _Chain:
    names: tuple[DomainName, ...]
    terminal: DomainName
    ttls: tuple[int, ...]


def _object(value: JsonValue) -> dict[str, JsonValue] | None:
    if not isinstance(value, dict):
        return None
    copied: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        copied[key] = item
    return copied


def _rcode(status: int) -> str:
    return _RCODE_NAME.get(status, str(status))


def _dns_name(text: str) -> DomainName | None:
    if text.endswith("."):
        text = text[:-1]
    name = text.lower()
    if not name or len(name) > _MAX_NAME or not all(_LABEL.fullmatch(label) for label in name.split(".")):
        return None
    return DomainName(name)


# A is IPv4; AAAA is IPv6 without a zone index and not IPv4-mapped
def _address(text: str, qtype: QueryType) -> IPAddress | None:
    try:
        parsed = ip_address(text)
    except ValueError:
        return None
    if parsed.version != qtype.version or (isinstance(parsed, IPv6Address) and (parsed.scope_id or parsed.ipv4_mapped is not None)):
        return None
    return parsed


def _status(body: Mapping[str, JsonValue]) -> int | DohError:
    status = body.get("Status")
    if type(status) is not int or not 0 <= status <= _MAX_RCODE:
        return DohError(DohProblem.MALFORMED, "Status must be an integer from 0 to 4095")
    return status


def _question(body: Mapping[str, JsonValue], name: DomainName, qtype: QueryType) -> DohError | None:
    question = body.get("Question")
    if not isinstance(question, list) or len(question) != 1:
        return DohError(DohProblem.QUESTION, "expected one question")
    item = _object(question[0])
    if item is None:
        return DohError(DohProblem.QUESTION, "question is not an object")
    qname = item.get("name")
    qrdtype = item.get("type")
    if not isinstance(qname, str) or type(qrdtype) is not int or _dns_name(qname) != name or qrdtype != qtype.rdtype:
        return DohError(DohProblem.QUESTION, "question does not match the asked name and type")
    return None


def _record(item: JsonValue) -> _Record | None:
    body = _object(item)
    if body is None:
        return None
    name = body.get("name")
    rdtype = body.get("type")
    ttl = body.get("TTL")
    data = body.get("data")
    if not isinstance(name, str) or not isinstance(data, str) or type(rdtype) is not int or type(ttl) is not int or not 0 <= ttl <= _MAX_TTL:
        return None
    return _Record(_dns_name(name), rdtype, data, ttl)


def _records(answer: JsonValue) -> tuple[_Record, ...] | DohError:
    if not isinstance(answer, list):
        return DohError(DohProblem.MALFORMED, "Answer is not an array")
    if len(answer) > MAX_RECORDS:
        return DohError(DohProblem.LIMIT, f"{len(answer)} records")
    records: list[_Record] = []
    for item in answer:
        record = _record(item)
        if record is None:
            return DohError(DohProblem.MALFORMED, "answer record is not a name, type, TTL and data")
        records.append(record)
    return tuple(records)


def _cnames(records: Sequence[_Record]) -> dict[DomainName, tuple[_Record, ...]]:
    found: dict[DomainName, list[_Record]] = {}
    for record in records:
        if record.rdtype == _CNAME and record.owner is not None:
            found.setdefault(record.owner, []).append(record)
    return {owner: tuple(links) for owner, links in found.items()}


# addresses belong to the name at the end of the chain; another name, a cycle or a fork is not a chain
def _follow(records: Sequence[_Record], start: DomainName) -> _Chain | DohError:
    links = _cnames(records)
    seen = {start}
    names: list[DomainName] = []
    ttls: list[int] = []
    current = start
    while current in links:
        targets: list[DomainName] = []
        for record in links[current]:
            target = _dns_name(record.data)
            if target is None:
                return DohError(DohProblem.CHAIN, f"invalid CNAME target {record.data!r}")
            targets.append(target)
        chosen = targets[0]
        if any(target != chosen for target in targets):
            return DohError(DohProblem.CHAIN, f"CNAME fork at {current}")
        if chosen in seen:
            return DohError(DohProblem.CHAIN, f"CNAME cycle at {chosen}")
        if len(names) == MAX_CHAIN:
            return DohError(DohProblem.CHAIN, f"CNAME chain is longer than {MAX_CHAIN}")
        names.append(chosen)
        ttls.extend(record.ttl for record in links[current])
        seen.add(chosen)
        current = chosen
    return _Chain(tuple(names), current, tuple(ttls))


def _addresses(records: Sequence[_Record], owner: DomainName, qtype: QueryType) -> tuple[tuple[IPAddress, ...], tuple[int, ...]] | DohError:
    found: list[IPAddress] = []
    seen: set[IPAddress] = set()
    ttls: list[int] = []
    for record in records:
        if record.owner != owner or record.rdtype != qtype.rdtype:
            continue
        parsed = _address(record.data, qtype)
        if parsed is None:
            return DohError(DohProblem.ADDRESS, f"invalid {qtype} address {record.data!r}")
        ttls.append(record.ttl)
        if parsed not in seen:
            seen.add(parsed)
            found.append(parsed)
    if len(found) > MAX_ADDRESSES:
        return DohError(DohProblem.LIMIT, f"{len(found)} addresses")
    return tuple(found), tuple(ttls)


def _ttl(values: Sequence[int]) -> Seconds | None:
    # the smallest TTL of the records that were used; callers do not store it
    return Seconds(min(values)) if values else None


def _outcome(records: Sequence[_Record], name: DomainName, qtype: QueryType, status: int) -> QueryResult | DohError:
    followed = _follow(records, name)
    if isinstance(followed, DohError):
        return followed
    found = _addresses(records, followed.terminal, qtype)
    if isinstance(found, DohError):
        return found
    addresses, address_ttls = found
    # NXDOMAIN cannot carry addresses; a bad address at the end of the chain is still a bad body
    if status == 3:
        return QueryResult(ResolveStatus.NXDOMAIN, cnames=followed.names, ttl=_ttl(followed.ttls))
    ttl = _ttl((*followed.ttls, *address_ttls))
    if addresses:
        return QueryResult(ResolveStatus.OK, addresses, followed.names, ttl)
    return QueryResult(ResolveStatus.NODATA, cnames=followed.names, ttl=ttl)


def parse_doh_json(name: DomainName, qtype: QueryType, payload: JsonValue) -> QueryResult | DohError:
    body = _object(payload)
    if body is None:
        return DohError(DohProblem.MALFORMED, "body is not a JSON object")
    status = _status(body)
    if isinstance(status, DohError):
        return status
    if (question := _question(body, name, qtype)) is not None:
        return question
    # an error rcode is the outcome; a broken Answer must not replace it
    if status not in (0, 3):
        return QueryResult(ResolveStatus.ERROR, error=_rcode(status))
    records = _records(body.get("Answer", []))
    if isinstance(records, DohError):
        return records
    return _outcome(records, name, qtype, status)


def _one_line(text: str) -> str:
    return " ".join(text.split())[:_MAX_ERROR]


def classify_doh(name: DomainName, qtype: QueryType, http_status: int | None, error: str | None, payload: JsonValue) -> QueryResult:
    if error is not None and (line := _one_line(error)):
        return QueryResult(ResolveStatus.ERROR, error=line)
    if http_status != 200:
        return QueryResult(ResolveStatus.ERROR, error=f"HTTP {http_status}")
    parsed = parse_doh_json(name, qtype, payload)
    return QueryResult(ResolveStatus.ERROR, error=str(parsed)) if isinstance(parsed, DohError) else parsed
