"""The authorisation boundary. Which supplier a request acts for comes from its session, never from anything the client sends."""
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Cookie, Depends, Header, HTTPException, Request

from tenderfit.auth import resolve_session
from tenderfit.db import Conn, connect

COOKIE = "tf_session"
CSRF_HEADER = "x-tenderfit"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def db() -> Iterator[Conn]:
    with connect() as conn:
        yield conn


@dataclass(frozen=True)
class Identity:
    user_id: int
    email: str
    token_hash: str
    profile_id: int | None


def check_csrf(request: Request, x_tenderfit: str | None = Header(default=None)) -> None:
    """A cross-site page cannot set a custom header without a CORS approval, and none is ever given."""
    if request.method not in SAFE_METHODS and not x_tenderfit:
        raise HTTPException(403, "this request is missing its X-TenderFit header")


def current_user(conn: Conn = Depends(db), tf_session: str | None = Cookie(default=None)) -> Identity:
    s = resolve_session(conn, tf_session)
    if not s:
        raise HTTPException(401, "sign in to continue", headers={"WWW-Authenticate": "Cookie"})
    return Identity(s["user_id"], s["email"], s["token_hash"], s["profile_id"])


def current_profile(me: Identity = Depends(current_user)) -> int:
    """The supplier this request acts for. Membership was re-checked against the database for this request."""
    if me.profile_id is None:
        raise HTTPException(403, "choose or create a supplier profile first")
    return me.profile_id
