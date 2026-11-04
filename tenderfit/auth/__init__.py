"""Accounts, sessions and supplier membership. Passwords use Argon2id through argon2-cffi; session tokens are random and stored hashed."""
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from tenderfit.config import settings
from tenderfit.db import Conn

_hasher = PasswordHasher()
# Verifying against this when the account is unknown keeps the response time the same, so timing does not reveal who has an account.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MIN_PASSWORD = 10


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    id: int
    email: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(conn: Conn, email: str, password: str) -> int:
    email = email.strip()
    if not EMAIL.fullmatch(email):
        raise AuthError("enter a valid email address")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"the password must have at least {MIN_PASSWORD} characters")
    if conn.execute("SELECT 1 FROM app_user WHERE lower(email) = lower(%s)", (email,)).fetchone():
        raise AuthError("that email address cannot be used")
    row = conn.execute("INSERT INTO app_user (email, password_hash) VALUES (%s, %s) RETURNING id", (email, _hasher.hash(password))).fetchone()
    assert row is not None
    return row["id"]


def _throttled(conn: Conn, email: str) -> bool:
    row = conn.execute("""SELECT count(*) AS n FROM login_failure WHERE lower(email) = lower(%s)
                          AND at > now() - make_interval(mins => %s)""", (email, settings.login_lockout_minutes)).fetchone()
    return row is not None and row["n"] >= settings.login_max_failures


def authenticate(conn: Conn, email: str, password: str) -> User | None:
    """The same outcome for a wrong password, an unknown account, a disabled account and too many attempts."""
    email = email.strip()
    row = conn.execute("SELECT id, email, password_hash, disabled_at FROM app_user WHERE lower(email) = lower(%s)", (email,)).fetchone()
    throttled = _throttled(conn, email)
    try:
        _hasher.verify(row["password_hash"] if row else _DUMMY_HASH, password)
        ok = row is not None and row["disabled_at"] is None and not throttled
    except (VerificationError, InvalidHashError):
        ok = False
    if not ok:
        conn.execute("INSERT INTO login_failure (email) VALUES (%s)", (email,))
        return None
    conn.execute("DELETE FROM login_failure WHERE lower(email) = lower(%s)", (email,))
    assert row is not None
    return User(row["id"], row["email"])


def memberships(conn: Conn, user_id: int) -> list[dict]:
    return conn.execute(
        """SELECT p.id, p.name, p.regions, p.description, p.is_fictional, m.role FROM profile_member m
           JOIN business_profile p ON p.id = m.profile_id WHERE m.user_id = %s ORDER BY p.id""", (user_id,)).fetchall()


def is_member(conn: Conn, user_id: int, profile_id: int) -> bool:
    return conn.execute("SELECT 1 FROM profile_member WHERE user_id = %s AND profile_id = %s", (user_id, profile_id)).fetchone() is not None


def grant(conn: Conn, user_id: int, profile_id: int, role: str = "owner") -> None:
    conn.execute("""INSERT INTO profile_member (user_id, profile_id, role) VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, profile_id) DO UPDATE SET role = EXCLUDED.role""", (user_id, profile_id, role))


def create_session(conn: Conn, user_id: int) -> str:
    """Returns the token for the cookie. A person with one supplier starts in it."""
    token = secrets.token_urlsafe(32)
    profiles = memberships(conn, user_id)
    conn.execute("""INSERT INTO user_session (token_hash, user_id, active_profile_id, expires_at) VALUES (%s, %s, %s, %s)""",
                 (hash_token(token), user_id, profiles[0]["id"] if len(profiles) == 1 else None,
                  datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)))
    conn.execute("DELETE FROM user_session WHERE expires_at < now()")
    return token


def resolve_session(conn: Conn, token: str | None) -> dict | None:
    """The live session for a token, with its active supplier only if membership still holds."""
    if not token:
        return None
    return conn.execute(
        """SELECT s.token_hash, u.id AS user_id, u.email,
                  CASE WHEN m.user_id IS NULL THEN NULL ELSE s.active_profile_id END AS profile_id
           FROM user_session s JOIN app_user u ON u.id = s.user_id
           LEFT JOIN profile_member m ON m.user_id = s.user_id AND m.profile_id = s.active_profile_id
           WHERE s.token_hash = %s AND s.expires_at > now() AND u.disabled_at IS NULL""", (hash_token(token),)).fetchone()


def set_active_profile(conn: Conn, token_hash: str, user_id: int, profile_id: int) -> bool:
    if not is_member(conn, user_id, profile_id):
        return False
    conn.execute("UPDATE user_session SET active_profile_id = %s WHERE token_hash = %s", (profile_id, token_hash))
    return True


def revoke_session(conn: Conn, token_hash: str) -> None:
    conn.execute("DELETE FROM user_session WHERE token_hash = %s", (token_hash,))
