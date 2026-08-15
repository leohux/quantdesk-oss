from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SECRET_KEY: str = os.getenv("SECRET_KEY", secrets.token_urlsafe(64))
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))
REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

# ---------------------------------------------------------------------------
# Password hashing (bcrypt directly, no passlib)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))

# ---------------------------------------------------------------------------
# In-memory user store (fail-closed: passwords MUST come from env)
# ---------------------------------------------------------------------------
_admin_pw = os.getenv("ADMIN_PASSWORD", "").strip()
_viewer_pw = os.getenv("VIEWER_PASSWORD", "").strip()
if not _admin_pw or len(_admin_pw) < 8:
    raise RuntimeError(
        "ADMIN_PASSWORD env var is required (min 8 characters, no default). "
        "Copy .env.example → .env and set ADMIN_PASSWORD before starting."
    )

ADMIN_USER: dict[str, Any] = {
    "username": "admin",
    "hashed_password": hash_password(_admin_pw),
    "role": "admin",
}

VIEWER_USER: dict[str, Any] = {
    "username": "viewer",
    "hashed_password": hash_password(_viewer_pw or _admin_pw),
    "role": "viewer",
}

USERS_DB: dict[str, dict[str, Any]] = {
    ADMIN_USER["username"]: ADMIN_USER,
    VIEWER_USER["username"]: VIEWER_USER,
}

# Role hierarchy: higher value = more privileges
ROLE_HIERARCHY: dict[str, int] = {
    "viewer": 1,
    "admin": 2,
}

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str

class RefreshRequest(BaseModel):
    refresh_token: str

class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict | None:
    """Decode and verify a JWT. Returns the payload dict or *None* on failure."""
    try:
        payload: dict = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def get_static_access_token() -> str:
    """Legacy ACCESS_TOKEN from env or settings store (auto-created if missing)."""
    env_token = os.getenv("ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        from config.store import get_raw_settings, save_app_settings

        raw = get_raw_settings()
        token = (raw.get("access_token") or "").strip()
        if not token:
            token = secrets.token_urlsafe(24)
            save_app_settings({"access_token": token})
        return token
    except Exception:
        return ""


def authenticate_token(token: str | None) -> dict[str, Any] | None:
    """Accept JWT access token OR legacy static ACCESS_TOKEN.

    Returns ``{sub, role, auth}`` or ``None``. Used by HTTP middleware and WS.
    """
    token = (token or "").strip()
    if not token:
        return None

    expected = get_static_access_token()
    if expected and token == expected:
        return {"sub": "legacy", "role": "admin", "auth": "legacy"}

    payload = verify_token(token)
    if (
        payload
        and payload.get("type") == "access"
        and payload.get("sub")
    ):
        return {
            "sub": payload.get("sub"),
            "role": payload.get("role"),
            "auth": "jwt",
        }
    return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict:
    """FastAPI dependency: reads Bearer header or ?token= query param."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token: str | None = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    else:
        token = request.query_params.get("token")

    if not token:
        raise credentials_exception

    # Prefer JWT user lookup so role_required still works; fall back to legacy.
    payload = verify_token(token)
    if payload and payload.get("type") == "access" and payload.get("sub"):
        username: str | None = payload.get("sub")
        user = USERS_DB.get(username) if username else None
        if user is not None:
            return {"sub": user["username"], "role": user["role"]}

    auth = authenticate_token(token)
    if auth and auth.get("auth") == "legacy":
        return {"sub": "legacy", "role": "admin"}

    raise credentials_exception


def role_required(required_role: str):
    """FastAPI dependency that enforces minimum role level."""
    required_level = ROLE_HIERARCHY.get(required_role, 0)

    async def _check(current_user: dict = Depends(get_current_user)) -> dict:
        user_level = ROLE_HIERARCHY.get(current_user.get("role", ""), 0)
        if user_level < required_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.get('role')}' insufficient; '{required_role}' required.",
            )
        return current_user

    return _check


# ---------------------------------------------------------------------------
# Auth handlers
# ---------------------------------------------------------------------------

async def login(body: LoginRequest) -> LoginResponse:
    """Authenticate and return access + refresh tokens."""
    user = USERS_DB.get(body.username)
    if user is None or not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_data = {"sub": user["username"], "role": user["role"]}
    return LoginResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token(token_data),
        role=user["role"],
    )


async def refresh(body: RefreshRequest) -> RefreshResponse:
    """Issue a new access token from a valid refresh token."""
    payload = verify_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    username: str | None = payload.get("sub")
    user = USERS_DB.get(username) if username else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    token_data = {"sub": user["username"], "role": user["role"]}
    return RefreshResponse(access_token=create_access_token(token_data))
