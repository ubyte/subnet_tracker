import sqlite3
from collections.abc import Mapping
from dataclasses import fields
from enum import Enum, auto
from typing import TYPE_CHECKING

from subnet_tracker.types import PREFIX_V4, PREFIX_V6, FieldError, Timestamp, check_prefix

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


class Keep(Enum):
    KEEP = auto()


KEEP = Keep.KEEP


def prefixes_error(prefix_v4: int | Keep | None, prefix_v6: int | Keep | None) -> FieldError | None:
    for field, prefix, limits in (("prefix_v4", prefix_v4, PREFIX_V4), ("prefix_v6", prefix_v6, PREFIX_V6)):
        if not isinstance(prefix, Keep) and (error := check_prefix(field, prefix, limits)):
            return error
    return None


def edit_columns(edit: "DataclassInstance") -> dict[str, object]:
    values = {f.name: getattr(edit, f.name) for f in fields(edit)}
    return {column: int(v) if isinstance(v, bool) else v for column, v in values.items() if not isinstance(v, Keep)}


def taken(conn: sqlite3.Connection, table: str, column: str, what: str, value: str | Keep, own_id: int | None) -> FieldError | None:
    if isinstance(value, str) and conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ? AND id IS NOT ?", (value, own_id)).fetchone():
        return FieldError(column, f"{what} {value!r} already exists")
    return None


def only_linked(links: str, member: str, column: str) -> str:
    return f"SELECT {member} FROM {links} GROUP BY {member} HAVING count(*) = 1 AND max({column}) = ?"


def insert_row(conn: sqlite3.Connection, table: str, columns: Mapping[str, object], now: Timestamp) -> sqlite3.Row:
    names = [*columns, "created_at", "updated_at"]
    row: sqlite3.Row = conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))}) RETURNING *", (*columns.values(), now, now)).fetchone()
    return row


def update_row(conn: sqlite3.Connection, table: str, row_id: int, columns: Mapping[str, object], now: Timestamp) -> None:
    if columns:
        assignments = ", ".join(f"{column} = ?" for column in columns)
        conn.execute(f"UPDATE {table} SET {assignments}, updated_at = ? WHERE id = ?", (*columns.values(), now, row_id))
