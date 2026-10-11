import errno
from collections import deque
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address

import dns.exception
import dns.message
import dns.name
import dns.rcode
import dns.rdata
import dns.rdataclass
import dns.rdatatype
from dns.rdtypes.ANY.CNAME import CNAME
from dns.rdtypes.IN.A import A
from dns.rdtypes.IN.AAAA import AAAA

from subnet_tracker.types import IPAddress, ResolveStatus, has_line_break

_MAX_ANSWER_RECORDS = 256
_MAX_ADDRESSES = 64
_MAX_CNAME_LINKS = 16
_MAX_TTL = 2_147_483_647
_MAX_REASON = 200
_UNREACHABLE = {
    errno.ENETUNREACH: "ENETUNREACH",
    errno.EHOSTUNREACH: "EHOSTUNREACH",
    errno.ENONET: "ENONET",
    errno.ENETDOWN: "ENETDOWN",
    errno.EHOSTDOWN: "EHOSTDOWN",
    errno.EAFNOSUPPORT: "EAFNOSUPPORT",
    errno.EADDRNOTAVAIL: "EADDRNOTAVAIL",
}
_AUTHORITATIVE = frozenset({ResolveStatus.OK, ResolveStatus.NODATA, ResolveStatus.NXDOMAIN, ResolveStatus.NOTFOUND})


@dataclass(frozen=True, slots=True)
class Classified:
    status: ResolveStatus
    addresses: tuple[IPAddress, ...] = ()
    cnames: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.status is ResolveStatus.OK) != bool(self.addresses):
            raise ValueError("ok is the status that carries addresses")
        if self.error is not None and (len(self.error) > _MAX_REASON or has_line_break(self.error)):
            raise ValueError("error text must be one line of at most 200 characters")


def classify_answer(name: str, qtype: str, message: dns.message.Message | None, failure: BaseException | None) -> Classified:
    if failure is not None:
        return _classify_failure(failure)
    if message is None:
        raise ValueError("a message or a transport failure is required")
    rdtype = _query_type(qtype)
    qname = _query_name(name)
    if not _question_matches(message, qname, rdtype):
        return _error("question does not match")
    rcode = message.rcode()
    if rcode not in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN):
        return _error(dns.rcode.to_text(rcode))
    if _answer_records(message) > _MAX_ANSWER_RECORDS:
        return _error("more than 256 answer records")
    chain = _chain(message, qname)
    if isinstance(chain, str):
        return _error(chain)
    cnames, terminal = chain
    if rcode == dns.rcode.NXDOMAIN:
        return Classified(ResolveStatus.NXDOMAIN, cnames=cnames)
    addresses = _addresses(message, terminal, rdtype)
    if isinstance(addresses, str):
        return _error(addresses, cnames)
    if addresses:
        return Classified(ResolveStatus.OK, addresses, cnames)
    return Classified(ResolveStatus.NODATA, cnames=cnames)


def classify_pair(a: Classified, aaaa: Classified) -> Classified:
    # One error column on the pair: the first failure text in A, then AAAA order.
    cnames = _merge(a.cnames, aaaa.cnames)
    if a.addresses or aaaa.addresses:
        if a.addresses and aaaa.addresses:
            error = None
        elif a.addresses:
            error = _failure_text(aaaa)
        else:
            error = _failure_text(a)
        return Classified(ResolveStatus.OK, _merge(a.addresses, aaaa.addresses), cnames, error)
    if a.status not in _AUTHORITATIVE or aaaa.status not in _AUTHORITATIVE:
        status = ResolveStatus.UNREACHABLE if a.status is ResolveStatus.UNREACHABLE and aaaa.status is ResolveStatus.UNREACHABLE else ResolveStatus.ERROR
        return Classified(status, cnames=cnames, error=_first_failure_text(a, aaaa))
    status = a.status if a.status is aaaa.status else ResolveStatus.NODATA
    return Classified(status, cnames=cnames)


def _classify_failure(failure: BaseException) -> Classified:
    for exc in _iter_exceptions(failure):
        if _timeout(exc):
            return Classified(ResolveStatus.ERROR, error="timeout")
        if (reason := _unreachable(exc)) is not None:
            return Classified(ResolveStatus.UNREACHABLE, error=reason)
        if isinstance(exc, OSError | dns.exception.DNSException):
            return Classified(ResolveStatus.ERROR, error=_one_line(str(exc)))
    return Classified(ResolveStatus.ERROR, error=_one_line(str(failure)))


def _iter_exceptions(failure: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    pending: deque[BaseException] = deque([failure])
    while pending:
        current = pending.popleft()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if (cause := current.__cause__) is not None:
            pending.append(cause)
        if (context := current.__context__) is not None and not current.__suppress_context__:
            pending.append(context)


def _timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError | dns.exception.Timeout) or (isinstance(exc, OSError) and exc.errno == errno.ETIMEDOUT)


def _unreachable(exc: BaseException) -> str | None:
    if isinstance(exc, OSError) and exc.errno is not None:
        return _UNREACHABLE.get(exc.errno)
    return None


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    if not line:
        return "error"
    return line[:_MAX_REASON]


def _query_type(text: str) -> dns.rdatatype.RdataType:
    try:
        rdtype = dns.rdatatype.from_text(text)
    except dns.exception.DNSException as exc:
        raise ValueError(f"unsupported query type {text!r}") from exc
    if rdtype not in (dns.rdatatype.A, dns.rdatatype.AAAA):
        raise ValueError(f"unsupported query type {text!r}")
    return rdtype


def _query_name(text: str) -> dns.name.Name:
    return _absolute(dns.name.from_text(text))


def _absolute(name: dns.name.Name) -> dns.name.Name:
    return name if name.is_absolute() else name.concatenate(dns.name.root)


def _name_text(name: dns.name.Name) -> str:
    return _absolute(name).canonicalize().to_text(omit_final_dot=True)


def _question_matches(message: dns.message.Message, qname: dns.name.Name, rdtype: dns.rdatatype.RdataType) -> bool:
    if len(message.question) != 1:
        return False
    question = message.question[0]
    return question.rdtype == rdtype and _absolute(question.name) == qname


def _answer_records(message: dns.message.Message) -> int:
    return sum(len(rrset) for rrset in message.answer)


def _error(reason: str, cnames: tuple[str, ...] = ()) -> Classified:
    return Classified(ResolveStatus.ERROR, cnames=cnames, error=_one_line(reason))


def _chain(message: dns.message.Message, qname: dns.name.Name) -> tuple[tuple[str, ...], dns.name.Name] | str:
    seen = {qname}
    cnames: list[str] = []
    current = qname
    links = 0
    while True:
        records = _matching(message, current, dns.rdatatype.CNAME)
        if not records:
            return (tuple(cnames), current)
        targets: list[dns.name.Name] = []
        known: set[dns.name.Name] = set()
        for ttl, rd in records:
            if not _ttl_ok(ttl):
                return "ttl out of range"
            if not isinstance(rd, CNAME):
                return "broken cname chain"
            target = _absolute(rd.target)
            if target not in known:
                known.add(target)
                targets.append(target)
        if len(targets) != 1:
            return "cname fork" if targets else "broken cname chain"
        links += 1
        if links > _MAX_CNAME_LINKS:
            return "cname chain is longer than 16"
        target = targets[0]
        if target in seen:
            return "cname cycle"
        seen.add(target)
        cnames.append(_name_text(target))
        current = target


def _matching(message: dns.message.Message, owner: dns.name.Name, rdtype: dns.rdatatype.RdataType) -> list[tuple[int, dns.rdata.Rdata]]:
    found: list[tuple[int, dns.rdata.Rdata]] = []
    for rrset in message.answer:
        if rrset.rdclass != dns.rdataclass.IN or rrset.rdtype != rdtype or _absolute(rrset.name) != owner:
            continue
        found.extend((rrset.ttl, rd) for rd in rrset)
    return found


def _ttl_ok(ttl: int) -> bool:
    return type(ttl) is int and 0 <= ttl <= _MAX_TTL


def _addresses(message: dns.message.Message, owner: dns.name.Name, rdtype: dns.rdatatype.RdataType) -> tuple[IPAddress, ...] | str:
    found: list[IPAddress] = []
    seen: set[IPAddress] = set()
    for ttl, rd in _matching(message, owner, rdtype):
        if not _ttl_ok(ttl):
            return "ttl out of range"
        parsed = _parse_ip(rdtype, rd)
        if isinstance(parsed, str):
            return parsed
        if parsed in seen:
            continue
        if len(seen) == _MAX_ADDRESSES:
            return "more than 64 addresses"
        seen.add(parsed)
        found.append(parsed)
    return tuple(found)


def _parse_ip(rdtype: dns.rdatatype.RdataType, rd: dns.rdata.Rdata) -> IPAddress | str:
    if rdtype == dns.rdatatype.A and isinstance(rd, A):
        text = rd.address
        want_v4 = True
    elif rdtype == dns.rdatatype.AAAA and isinstance(rd, AAAA):
        text = rd.address
        want_v4 = False
    else:
        return "invalid address"
    if not isinstance(text, str):
        return "invalid address"
    try:
        ip = ip_address(text)
    except ValueError:
        return "invalid address"
    if want_v4:
        return ip if isinstance(ip, IPv4Address) else "invalid address"
    if not isinstance(ip, IPv6Address):
        return "invalid address"
    if ip.scope_id is not None:
        return "zone index is not routable"
    if ip.ipv4_mapped is not None:
        return "IPv4-mapped addresses are not routable, use the IPv4 form"
    return ip


def _merge[T: Hashable](first: tuple[T, ...], second: tuple[T, ...]) -> tuple[T, ...]:
    seen: set[T] = set()
    merged: list[T] = []
    for item in (*first, *second):
        if item not in seen:
            seen.add(item)
            merged.append(item)
    return tuple(merged)


def _failure_text(answer: Classified) -> str | None:
    if answer.status in (ResolveStatus.ERROR, ResolveStatus.UNREACHABLE):
        return answer.error
    return None


def _first_failure_text(a: Classified, aaaa: Classified) -> str | None:
    for answer in (a, aaaa):
        if answer.status not in _AUTHORITATIVE and answer.error is not None:
            return answer.error
    return None
