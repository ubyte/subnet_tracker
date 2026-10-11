import sqlite3
from dataclasses import dataclass

from subnet_tracker.discovery.www import DomainNames
from subnet_tracker.types import DomainId, DomainName, NameId, Timestamp

NAME_COLUMNS = "n.id AS name_id, n.domain_id, n.name, n.canonical, n.last_attempt_at, n.last_success_at"


@dataclass(frozen=True, slots=True)
class Name:
    id: NameId
    domain_id: DomainId
    name: DomainName
    canonical: bool
    last_attempt_at: Timestamp | None
    last_success_at: Timestamp | None


def name_from_row(row: sqlite3.Row) -> Name:
    return Name(row["name_id"], row["domain_id"], row["name"], bool(row["canonical"]), row["last_attempt_at"], row["last_success_at"])


def insert_names(conn: sqlite3.Connection, domain_id: DomainId, names: DomainNames) -> None:
    conn.executemany("INSERT INTO names (domain_id, name, canonical) VALUES (?, ?, ?)", [(domain_id, names.canonical, 1), (domain_id, names.pair, 0)])


def tracked_names(conn: sqlite3.Connection) -> frozenset[DomainName]:
    return frozenset(DomainName(name) for (name,) in conn.execute("SELECT name FROM names"))
