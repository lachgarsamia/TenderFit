"""What the what-if agent may spend, per supplier, over any rolling 24 hours. Reads and an advisory lock only.

Spend is the sum of simulation_agent_step.cost_usd for the supplier's simulations: actual usage for a call that
answered, an estimate for one that failed after it may have been billed (output.cost_estimated). Steps are never
updated or deleted while the simulation exists (020's triggers; a discard keeps them), so the account cannot be
lowered by discarding a simulation.

Two checks use it, both under one per-supplier advisory lock so they cannot interleave:
  admission   before a job is queued: the day's spend plus a whole session's cap must fit the daily allowance;
  each call   before every model call: the day's spend, and the session's own, plus that call's worst case must fit.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta

from tenderfit.config import settings
from tenderfit.db import Conn, must
from tenderfit.llm import PRICES

SPEND_LOCK = 0x535350  # "SSP": the first key of the two-key advisory lock; the second is the supplier
WINDOW = timedelta(hours=24)
CHARS_PER_TOKEN = 3     # conservative: real prompts average nearer 4, so this overestimates input
MIN_CALL_S = 15         # no call starts with less time than this left in the session
CALL_MARGIN_S = 5       # a call's timeout leaves this much of the session for recording its result


def _key(profile_id: int) -> tuple[int, int]:
    return SPEND_LOCK, profile_id % 2147483647


@contextmanager
def supplier_lock(conn: Conn, profile_id: int):
    """A session-level lock: held across commits, as admission's create-then-enqueue needs. Blocks until free. The
    caller commits what it means to keep; anything left uncommitted on exit is rolled back before the lock is released."""
    conn.execute("SELECT pg_advisory_lock(%s, %s)", _key(profile_id))
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s, %s)", _key(profile_id))
        conn.commit()


def lock_for_transaction(conn: Conn, profile_id: int) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", _key(profile_id))


def spent_24h(conn: Conn, profile_id: int) -> float:
    return float(must(conn.execute(
        """SELECT coalesce(sum(st.cost_usd), 0) AS spent FROM simulation_agent_step st JOIN simulation s ON s.id = st.simulation_id
           WHERE s.profile_id = %s AND st.created_at > now() - %s""", (profile_id, WINDOW)).fetchone())["spent"])


def session_spent(conn: Conn, simulation_id: int) -> float:
    return float(must(conn.execute("SELECT coalesce(sum(cost_usd), 0) AS spent FROM simulation_agent_step WHERE simulation_id = %s",
                                   (simulation_id,)).fetchone())["spent"])


def frees_at(conn: Conn, profile_id: int, needed: float) -> datetime | None:
    """When enough of the day's spend leaves the window for `needed` more to fit, or None if it already fits."""
    spent, daily = spent_24h(conn, profile_id), settings.simulation_agent_daily_usd
    if spent + needed <= daily:
        return None
    over = spent + needed - daily
    rows = conn.execute(
        """SELECT st.created_at, st.cost_usd FROM simulation_agent_step st JOIN simulation s ON s.id = st.simulation_id
           WHERE s.profile_id = %s AND st.created_at > now() - %s AND st.cost_usd > 0 ORDER BY st.created_at, st.id""",
        (profile_id, WINDOW)).fetchall()
    released = 0.0
    for r in rows:
        released += float(r["cost_usd"])
        if released >= over - 1e-9:
            return r["created_at"] + WINDOW
    return None if not rows else rows[-1]["created_at"] + WINDOW


def allowance(conn: Conn, profile_id: int) -> dict:
    """What the supplier has left today, for the API. Reads only."""
    spent, daily, session = spent_24h(conn, profile_id), settings.simulation_agent_daily_usd, settings.simulation_agent_session_usd
    nxt = frees_at(conn, profile_id, session)
    return {"daily_usd": daily, "session_usd": session, "spent_24h_usd": round(spent, 6),
            "remaining_usd": round(max(daily - spent, 0.0), 6), "can_ask": nxt is None,
            "next_question_at": nxt.isoformat() if nxt else None}


def _prices(model: str) -> tuple[float, float]:
    """Per token, input and output. An unpriced model is costed as the most expensive one: never as free."""
    i, o, _ = PRICES.get(model) or max(PRICES.values(), key=lambda p: p[1])
    return i / 1e6, o / 1e6


def input_tokens_estimate(system: str, messages: list[dict], tools: list[dict]) -> int:
    return len(json.dumps([system, messages, tools], default=str)) // CHARS_PER_TOKEN + 1


def worst_case(model: str, input_tokens: int, max_tokens: int) -> float:
    i, o = _prices(model)
    return input_tokens * i + max_tokens * o


def admit_call(conn: Conn, profile_id: int, simulation_id: int, cost: float) -> str | None:
    """Whether a call that could cost `cost` may start. None if it may, else why not. Its own transaction."""
    try:
        lock_for_transaction(conn, profile_id)
        day, own = spent_24h(conn, profile_id), session_spent(conn, simulation_id)
    finally:
        conn.rollback()
    if day + cost > settings.simulation_agent_daily_usd:
        return f"spend limit: the supplier's daily allowance of ${settings.simulation_agent_daily_usd:.2f} would be exceeded"
    if own + cost > settings.simulation_agent_session_usd:
        return f"spend limit: this question's allowance of ${settings.simulation_agent_session_usd:.2f} would be exceeded"
    return None
