import sqlite3

from subnet_tracker.core.batches import get_batch, undo_batch
from subnet_tracker.core.db import transaction
from subnet_tracker.core.domains import Domain
from subnet_tracker.core.services.domains import record_deleted
from subnet_tracker.types import BatchId, Timestamp


# the domains of the batch that still existed; None for an unknown batch
def undo_add(conn: sqlite3.Connection, batch_id: BatchId, actor: str, now: Timestamp) -> tuple[Domain, ...] | None:
    with transaction(conn):
        summary = get_batch(conn, batch_id)
        if summary is None:
            return None
        undo_batch(conn, batch_id, now)
        record_deleted(conn, summary.domains, actor, now, batch_id)
    return summary.domains
