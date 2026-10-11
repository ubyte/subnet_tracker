from collections.abc import Sequence

from subnet_tracker.cli.output import fields, listed, moment, table
from subnet_tracker.core.domain_view import AddressView, DomainView, NameView
from subnet_tracker.core.domains import Domain
from subnet_tracker.core.name_status import NameStatus, ResolveResult, address_state
from subnet_tracker.types import JsonValue, Seconds

LIST_HEADERS = ("domain", "groups", "tags", "prefix", "routed", "name", "status", "success", "attempt")


def yes(flag: bool) -> str:
    return "yes" if flag else "no"


def status_text(status: NameStatus | None) -> str:
    return status or "-"


def pair_text(domain: Domain) -> str:
    return f"{domain.canonical.name} ({domain.pair.name})"


# the domain's own prefixes in the export notation, inherited ones are not shown
def prefixes(domain: Domain) -> str:
    own = [f"/{domain.prefix_v4}" if domain.prefix_v4 is not None else "", f"v6/{domain.prefix_v6}" if domain.prefix_v6 is not None else ""]
    return " ".join(p for p in own if p) or "-"


def _name_cells(n: NameView) -> tuple[str, str, str, str]:
    return n.name.name, status_text(n.status), moment(n.name.last_success_at), moment(n.name.last_attempt_at)


def domains_table(views: Sequence[DomainView]) -> str:
    rows: list[tuple[str, ...]] = []
    for v in views:
        canonical, pair = v.names
        rows += [(v.domain.canonical.name, listed(v.groups), listed(v.tags), prefixes(v.domain), yes(v.routable), *_name_cells(canonical)), ("",) * 5 + _name_cells(pair)]
    return table(LIST_HEADERS, rows)


def _indented(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())


def _source(d: Domain) -> str:
    return ", ".join([d.source, *([f"batch {d.batch_id}"] if d.batch_id is not None else []), *([d.source_url] if d.source_url else [])])


def _address_row(a: AddressView, retention: Seconds) -> tuple[str, ...]:
    first, last = min(s.first_seen_at for s in a.sightings), max(s.last_seen_at for s in a.sightings)
    return a.ip, address_state(a.sightings, retention), ", ".join(s.resolver for s in a.sightings), moment(first), moment(last)


def name_text(n: NameView, retention: Seconds) -> str:
    lines = [f"{n.name.name}: {status_text(n.status)}, success {moment(n.name.last_success_at)}, attempt {moment(n.name.last_attempt_at)}"]
    if n.results:
        rows = [(r.resolver, yes(r.routes), r.status, moment(r.succeeded_at), moment(r.attempted_at), r.error or "") for r in n.results]
        lines.append(_indented(table(("resolver", "routes", "status", "success", "attempt", "error"), rows)))
    if n.addresses:
        lines.append(_indented(table(("address", "state", "seen by", "first", "last"), [_address_row(a, retention) for a in n.addresses])))
    return "\n".join(lines)


def domain_text(view: DomainView, retention: Seconds) -> str:
    d = view.domain
    settings = [("domain", d.canonical.name), ("pair", d.pair.name), ("groups", listed(view.groups)), ("tags", listed(view.tags)), ("prefix", prefixes(d))]
    state = [("enabled", yes(d.enabled)), ("routed", yes(view.routable)), ("comment", d.comment), ("source", _source(d)), ("added", moment(d.created_at)), ("updated", moment(d.updated_at))]
    return "\n\n".join([fields(settings + state), *(name_text(n, retention) for n in view.names)])


def _result_json(r: ResolveResult) -> JsonValue:
    return {"resolver": r.resolver, "routes": r.routes, "status": r.status, "attempted_at": r.attempted_at, "answered_at": r.answered_at, "succeeded_at": r.succeeded_at, "error": r.error}


def _address_json(a: AddressView, retention: Seconds) -> JsonValue:
    return {"ip": a.ip, "state": address_state(a.sightings, retention), "sightings": [{"resolver": s.resolver, "first_seen_at": s.first_seen_at, "last_seen_at": s.last_seen_at} for s in a.sightings]}


def name_json(n: NameView, retention: Seconds | None) -> JsonValue:
    data: dict[str, JsonValue] = {"name": n.name.name, "status": n.status, "last_attempt_at": n.name.last_attempt_at, "last_success_at": n.name.last_success_at}
    if retention is None:
        return data
    return data | {"results": [_result_json(r) for r in n.results], "addresses": [_address_json(a, retention) for a in n.addresses]}


# with retention the names carry their resolver results and addresses
def domain_json(view: DomainView, retention: Seconds | None = None) -> JsonValue:
    d = view.domain
    return {
        "id": d.id,
        "name": d.canonical.name,
        "pair": d.pair.name,
        "groups": list(view.groups),
        "tags": list(view.tags),
        "prefix_v4": d.prefix_v4,
        "prefix_v6": d.prefix_v6,
        "enabled": d.enabled,
        "routed": view.routable,
        "comment": d.comment,
        "source": d.source,
        "source_url": d.source_url,
        "batch_id": d.batch_id,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
        "status": view.status,
        "names": [name_json(n, retention) for n in view.names],
    }
