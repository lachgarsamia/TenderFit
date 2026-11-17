"""Database-enforced isolation: work that must not write anything runs where Postgres refuses every write."""
from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import IsolationLevel
from psycopg.pq import TransactionStatus

from tenderfit.db import Conn


def require_idle(conn: Conn) -> None:
    """Simulation functions own their transactions. They never commit, or roll back, work the caller has not finished."""
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("finish or roll back the open transaction before working with simulations")


@contextmanager
def read_only(conn: Conn) -> Iterator[None]:
    """Everything inside runs in a READ ONLY transaction: an INSERT or UPDATE anywhere, even deep in reused assessment code,
    raises ReadOnlySqlTransaction instead of changing live data. The transaction always ends in a rollback.

    It is also REPEATABLE READ: every statement sees the same snapshot of the database, so values read early (control) and
    late (simulated, and the provenance recorded with them) cannot differ because someone committed in between. A read-only
    transaction never fails with a serialization error, so this costs nothing."""
    require_idle(conn)
    before, level = conn.read_only, conn.isolation_level
    conn.read_only, conn.isolation_level = True, IsolationLevel.REPEATABLE_READ
    try:
        yield
    finally:
        conn.rollback()
        conn.read_only, conn.isolation_level = before, level
