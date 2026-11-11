from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from tenderfit import auth
from tenderfit.api.deps import COOKIE, Identity, current_profile, current_user, db
from tenderfit.config import settings
from tenderfit.db import Conn
from tenderfit.profile import service as profiles

router = APIRouter(tags=["accounts"])


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=settings.session_cookie_secure,
                        max_age=settings.session_hours * 3600, path="/")


def _me(conn: Conn, user_id: int, email: str, profile_id: int | None) -> dict:
    return {"email": email, "profiles": auth.memberships(conn, user_id), "active_profile_id": profile_id}


@router.get("/auth/config")
def auth_config() -> dict:
    return {"registration_enabled": settings.registration_enabled, "demo": settings.demo_scripted_llm}


@router.post("/auth/register", status_code=201)
def register(body: Credentials, response: Response, conn: Conn = Depends(db)) -> dict:
    if not settings.registration_enabled:
        raise HTTPException(403, "registration is switched off; ask the operator for an account")
    try:
        user_id = auth.create_user(conn, body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(422, str(e))
    _set_cookie(response, auth.create_session(conn, user_id))
    conn.commit()
    return _me(conn, user_id, body.email.strip(), None)


@router.post("/auth/login")
def login(body: Credentials, response: Response, conn: Conn = Depends(db)) -> dict:
    user = auth.authenticate(conn, body.email, body.password)
    conn.commit()
    if not user:
        raise HTTPException(401, "that email and password do not match, or too many attempts were made; try again later")
    token = auth.create_session(conn, user.id)
    conn.commit()
    _set_cookie(response, token)
    session = auth.resolve_session(conn, token)
    assert session is not None
    return _me(conn, user.id, user.email, session["profile_id"])


@router.post("/auth/logout")
def logout(response: Response, me: Identity = Depends(current_user), conn: Conn = Depends(db)) -> dict:
    auth.revoke_session(conn, me.token_hash)
    conn.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"signed_out": True}


@router.get("/auth/me")
def who_am_i(me: Identity = Depends(current_user), conn: Conn = Depends(db)) -> dict:
    return _me(conn, me.user_id, me.email, me.profile_id)


class ActiveProfile(BaseModel):
    profile_id: int


@router.post("/auth/active-profile")
def choose_profile(body: ActiveProfile, me: Identity = Depends(current_user), conn: Conn = Depends(db)) -> dict:
    if not auth.set_active_profile(conn, me.token_hash, me.user_id, body.profile_id):
        raise HTTPException(404, "profile not found")
    conn.commit()
    return _me(conn, me.user_id, me.email, body.profile_id)


class ProfileIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    regions: list[str] = Field(default_factory=list, max_length=20)
    description: str = Field(default="", max_length=2000)


@router.get("/profiles")
def list_profiles(me: Identity = Depends(current_user), conn: Conn = Depends(db)) -> list[dict]:
    return auth.memberships(conn, me.user_id)


@router.post("/profiles", status_code=201)
def create_profile(body: ProfileIn, me: Identity = Depends(current_user), conn: Conn = Depends(db)) -> dict:
    try:
        pid = profiles.create_profile(conn, body.name, body.regions, body.description)
    except profiles.ProfileError as e:
        raise HTTPException(422, str(e))
    auth.grant(conn, me.user_id, pid)
    auth.set_active_profile(conn, me.token_hash, me.user_id, pid)
    conn.commit()
    return {"id": pid, **_me(conn, me.user_id, me.email, pid)}


@router.put("/profile")
def edit_profile(body: ProfileIn, profile_id: int = Depends(current_profile), conn: Conn = Depends(db)) -> dict:
    try:
        profiles.edit_profile(conn, profile_id, body.name, body.regions, body.description)
    except profiles.ProfileError as e:
        raise HTTPException(422, str(e))
    conn.commit()
    return {"id": profile_id, "name": body.name.strip(), "regions": body.regions, "description": body.description}
