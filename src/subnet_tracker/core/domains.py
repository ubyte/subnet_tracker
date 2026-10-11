import json
import sqlite3
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, fields, replace
from enum import Enum, auto
from itertools import groupby
from operator import itemgetter

from subnet_tracker.core.names import NAME_COLUMNS, Name, insert_names, name_from_row
from subnet_tracker.discovery.www import DomainNames
from subnet_tracker.types import PREFIX_V4, PREFIX_V6, BatchId, DomainId, DomainName, DomainSource, FieldError, Timestamp, check_prefix, one_line_text

_SELECT = f"SELECT d.*, {NAME_COLUMNS} FROM domains d JOIN names n ON n.domain_id = d.id"
_ORDER = " ORDER BY d.id DESC, n.canonical DESC"
_IN_IDS = "id IN (SELECT value FROM json_each(?))"


class Keep(Enum):
    KEEP = auto()


KEEP = Keep.KEEP


@dataclass(frozen=True, slots=True)
class Domain:
    id: DomainId
    canonical: Name
    pair: Name
    prefix_v4: int | None
    prefix_v6: int | None
    enabled: bool
    comment: str
    source: DomainSource
    source_url: str | None
    batch_id: BatchId | None
    created_at: Timestamp
    updated_at: Timestamp

    @property
    def names(self) -> tuple[Name, Name]:
        return (self.canonical, self.pair)


@dataclass(frozen=True, slots=True)
class DomainEdit:
    # None sets the prefix back to inherited
    prefix_v4: int | Keep | None = KEEP
    prefix_v6: int | Keep | None = KEEP
    enabled: bool | Keep = KEEP
    comment: str | Keep = KEEP


def checked_edit(edit: DomainEdit) -> DomainEdit | FieldError:
    for field, prefix, limits in (("prefix_v4", edit.prefix_v4, PREFIX_V4), ("prefix_v6", edit.prefix_v6, PREFIX_V6)):
        if not isinstance(prefix, Keep) and (error := check_prefix(field, prefix, limits)):
            return error
    if isinstance(edit.comment, Keep):
        return edit
    comment = one_line_text("comment", edit.comment)
    return comment if isinstance(comment, FieldError) else replace(edit, comment=comment)


def _edit_columns(edit: DomainEdit) -> dict[str, object]:
    values = {f.name: getattr(edit, f.name) for f in fields(edit)}
    return {column: int(v) if isinstance(v, bool) else v for column, v in values.items() if not isinstance(v, Keep)}


def _domain(rows: Sequence[sqlite3.Row]) -> Domain:
    canonical, pair = (name_from_row(row) for row in rows)
    d = rows[0]
    return Domain(
        d["id"], canonical, pair, d["prefix_v4"], d["prefix_v6"], bool(d["enabled"]), d["comment"], DomainSource(d["source"]), d["source_url"], d["batch_id"], d["created_at"], d["updated_at"]
    )


def _domains(rows: Iterable[sqlite3.Row]) -> tuple[Domain, ...]:
    return tuple(_domain(list(group)) for _, group in groupby(rows, key=itemgetter("domain_id")))


def _ids(ids: Collection[DomainId]) -> str:
    return json.dumps(list(ids))


def insert_domain(conn: sqlite3.Connection, names: DomainNames, source: DomainSource, batch_id: BatchId | None, now: Timestamp) -> DomainId:
    domain_id = DomainId(conn.execute("INSERT INTO domains (source, batch_id, created_at, updated_at) VALUES (?, ?, ?, ?) RETURNING id", (source, batch_id, now, now)).fetchone()["id"])
    insert_names(conn, domain_id, names)
    return domain_id


def list_domains(conn: sqlite3.Connection, batch_id: BatchId | None = None) -> tuple[Domain, ...]:
    if batch_id is None:
        return _domains(conn.execute(_SELECT + _ORDER))
    return _domains(conn.execute(_SELECT + " WHERE d.batch_id = ?" + _ORDER, (batch_id,)))


def get_domain(conn: sqlite3.Connection, domain_id: DomainId) -> Domain | None:
    return next(iter(_domains(conn.execute(_SELECT + " WHERE d.id = ?" + _ORDER, (domain_id,)))), None)


def find_domain(conn: sqlite3.Connection, name: DomainName) -> Domain | None:
    return next(iter(_domains(conn.execute(_SELECT + " WHERE d.id = (SELECT domain_id FROM names WHERE name = ?)" + _ORDER, (name,)))), None)


def delete_domains(conn: sqlite3.Connection, ids: Collection[DomainId]) -> int:
    return conn.execute(f"DELETE FROM domains WHERE {_IN_IDS}", (_ids(ids),)).rowcount


def edit_domains(conn: sqlite3.Connection, ids: Collection[DomainId], edit: DomainEdit, now: Timestamp) -> int | FieldError:
    checked = checked_edit(edit)
    if isinstance(checked, FieldError):
        return checked
    if not (columns := _edit_columns(checked)):
        return 0
    assignments = ", ".join(f"{column} = ?" for column in columns)
    return conn.execute(f"UPDATE domains SET {assignments}, updated_at = ? WHERE {_IN_IDS}", (*columns.values(), now, _ids(ids))).rowcount
