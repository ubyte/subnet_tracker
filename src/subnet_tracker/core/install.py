import sqlite3

from subnet_tracker.core.db import migrate, transaction
from subnet_tracker.core.resolvers import create_default_resolvers
from subnet_tracker.types import Timestamp


def migrate_database(conn: sqlite3.Connection, now: Timestamp) -> tuple[int, ...]:
    with transaction(conn):
        applied = migrate(conn)
        # only an empty database starts from version 0, so the defaults are created once
        if applied[:1] == (1,):
            create_default_resolvers(conn, now)
    return applied
