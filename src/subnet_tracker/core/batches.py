import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from subnet_tracker.core.db import transaction
from subnet_tracker.core.domains import Domain, insert_domain, list_domains
from subnet_tracker.core.names import tracked_names
from subnet_tracker.discovery.parse import Network, parse_paste
from subnet_tracker.discovery.www import Deduplicated, DomainNames, deduplicate
from subnet_tracker.types import BatchId, BatchKind, DomainName, DomainSource, ParseError, Timestamp

MAX_INPUT_BYTES = 64 * 1024
_SOURCES = {BatchKind.PASTE: DomainSource.MANUAL, BatchKind.CLI: DomainSource.MANUAL, BatchKind.SCAN: DomainSource.DISCOVERED, BatchKind.IMPORT: DomainSource.IMPORT}
_SELECT = "SELECT id, created_at, kind, actor, input, added, duplicates, invalid, undone_at FROM add_batches"
_CANONICAL_NAMES = "SELECT n.name FROM domains d JOIN names n ON n.domain_id = d.id AND n.canonical = 1 WHERE d.batch_id = ? ORDER BY d.id"


@dataclass(frozen=True, slots=True)
class Batch:
    id: BatchId
    created_at: Timestamp
    kind: BatchKind
    actor: str
    input: str
    added: int
    duplicates: int
    invalid: tuple[ParseError, ...]
    undone_at: Timestamp | None


@dataclass(frozen=True, slots=True)
class BatchSummary:
    batch: Batch
    # domains of the batch that still exist, in input order
    domains: tuple[Domain, ...]


@dataclass(frozen=True, slots=True)
class AddResult:
    batch_id: BatchId
    added: tuple[DomainNames, ...]
    duplicates: tuple[DomainName, ...]
    invalid: tuple[ParseError, ...] = ()
    networks: tuple[Network, ...] = ()
    wildcards: tuple[DomainName, ...] = ()


def clip_input(text: str) -> str:
    # CLI arguments may carry lone surrogates that UTF-8 cannot encode
    return text.encode(errors="replace")[:MAX_INPUT_BYTES].decode(errors="ignore")


def _encode_invalid(errors: Sequence[ParseError]) -> str:
    return json.dumps([[e.line, e.message] for e in errors], ensure_ascii=False)


def _decode_invalid(raw: str) -> tuple[ParseError, ...]:
    return tuple(ParseError(line, message) for line, message in json.loads(raw))


def _batch(row: sqlite3.Row) -> Batch:
    return Batch(row["id"], row["created_at"], BatchKind(row["kind"]), row["actor"], row["input"], row["added"], row["duplicates"], _decode_invalid(row["invalid"]), row["undone_at"])


def _write(conn: sqlite3.Connection, kind: BatchKind, actor: str, text: str, found: Deduplicated, invalid: Sequence[ParseError], now: Timestamp) -> BatchId:
    row = (now, kind, actor, clip_input(text), len(found.new_domains), len(found.duplicates), _encode_invalid(invalid))
    batch_id = BatchId(conn.execute("INSERT INTO add_batches (created_at, kind, actor, input, added, duplicates, invalid) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id", row).fetchone()["id"])
    for names in found.new_domains:
        insert_domain(conn, names, _SOURCES[kind], batch_id, now)
    return batch_id


def add_text(conn: sqlite3.Connection, kind: BatchKind, actor: str, text: str, now: Timestamp) -> AddResult:
    with transaction(conn):
        parsed = parse_paste(text, tracked_names(conn))
        plan = parsed.value
        batch_id = _write(conn, kind, actor, text, Deduplicated(plan.new_domains, plan.duplicates), parsed.errors, now)
    return AddResult(batch_id, plan.new_domains, plan.duplicates, parsed.errors, plan.networks, plan.wildcards)


def add_names(conn: sqlite3.Connection, kind: BatchKind, actor: str, text: str, names: Iterable[DomainName], now: Timestamp) -> AddResult:
    with transaction(conn):
        found = deduplicate(names, tracked_names(conn))
        batch_id = _write(conn, kind, actor, text, found, (), now)
    return AddResult(batch_id, found.new_domains, found.duplicates)


def get_batch(conn: sqlite3.Connection, batch_id: BatchId) -> BatchSummary | None:
    row = conn.execute(_SELECT + " WHERE id = ?", (batch_id,)).fetchone()
    return None if row is None else BatchSummary(_batch(row), tuple(reversed(list_domains(conn, batch_id))))


def list_batches(conn: sqlite3.Connection, limit: int) -> tuple[Batch, ...]:
    return tuple(_batch(row) for row in conn.execute(_SELECT + " ORDER BY id DESC LIMIT ?", (limit,)))


def undo_batch(conn: sqlite3.Connection, batch_id: BatchId, now: Timestamp) -> tuple[DomainName, ...] | None:
    with transaction(conn):
        if not conn.execute("UPDATE add_batches SET undone_at = coalesce(undone_at, ?) WHERE id = ?", (now, batch_id)).rowcount:
            return None
        removed = tuple(DomainName(name) for (name,) in conn.execute(_CANONICAL_NAMES, (batch_id,)))
        conn.execute("DELETE FROM domains WHERE batch_id = ?", (batch_id,))
    return removed
