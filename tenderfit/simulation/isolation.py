"""Database-enforced isolation: work that must not write anything runs where Postgres refuses every write."""
from collections.abc import Iterator
from contextlib import contextmanager

from psycopg.pq import TransactionStatus

from tenderfit.db import Conn


def require_idle(conn: Conn) -> None:
    """Simulation functions own their transactions. They never commit, or roll back, work the caller has not finished."""
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("finish or roll back the open transaction before working with simulations")


@contextmanager
def read_only(conn: Conn) -> Iterator[None]:
    """Everything inside runs in a READ ONLY transaction: an INSERT or UPDATE anywhere, even deep in reused assessment code,
    raises ReadOnlySqlTransaction instead of changing live data. The transaction always ends in a rollback."""
    require_idle(conn)
    before = conn.read_only
    conn.read_only = True
    try:
        yield
    finally:
        conn.rollback()
        conn.read_only = before
