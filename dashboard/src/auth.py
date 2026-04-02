import os
import json
import logging
import secrets
from urllib.parse import urlencode

import httpx
from jose import jwt, JWTError
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer

logger = logging.getLogger("auth")

COGNITO_REGION = os.getenv("COGNITO_REGION", "ap-south-1")
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID", "")
COGNITO_CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET", "")
COGNITO_DOMAIN = os.getenv("COGNITO_DOMAIN", "")
APP_URL = os.getenv("APP_URL", "https://healing.agentichumans.in")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"

serializer = URLSafeTimedSerializer(SESSION_SECRET)

COGNITO_BASE_URL = f"https://{COGNITO_DOMAIN}.auth.{COGNITO_REGION}.amazoncognito.com"
COGNITO_JWKS_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"

_jwks_cache = None


async def get_jwks():
    global _jwks_cache
    if _jwks_cache is None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(COGNITO_JWKS_URL)
            _jwks_cache = resp.json()
    return _jwks_cache


def get_login_url(state: str = "") -> str:
    params = {
        "response_type": "code",
        "client_id": COGNITO_CLIENT_ID,
        "redirect_uri": f"{APP_URL}/auth/callback",
        "scope": "openid email profile",
        "state": state,
    }
    return f"{COGNITO_BASE_URL}/oauth2/authorize?{urlencode(params)}"


def get_logout_url() -> str:
    params = {
        "client_id": COGNITO_CLIENT_ID,
        "logout_uri": f"{APP_URL}/logout",
    }
    return f"{COGNITO_BASE_URL}/logout?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    token_url = f"{COGNITO_BASE_URL}/oauth2/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": COGNITO_CLIENT_ID,
        "client_secret": COGNITO_CLIENT_SECRET,
        "code": code,
        "redirect_uri": f"{APP_URL}/auth/callback",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(token_url, data=data)
        if resp.status_code != 200:
            logger.error(f"Token exchange failed: {resp.text}")
            raise HTTPException(status_code=401, detail="Authentication failed")
        return resp.json()


async def verify_token(id_token: str, access_token: str = "") -> dict:
    try:
        jwks = await get_jwks()
        header = jwt.get_unverified_header(id_token)
        kid = header.get("kid")

        key = None
        for k in jwks.get("keys", []):
            if k["kid"] == kid:
                key = k
                break

        if not key:
            raise JWTError("Key not found")

        options = {
            "verify_at_hash": False,
        }

        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=COGNITO_CLIENT_ID,
            issuer=f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}",
            access_token=access_token if access_token else None,
            options=options,
        )
        return claims
    except JWTError as e:
        logger.error(f"Token verification failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid token")


def create_session(email: str, name: str = "") -> str:
    return serializer.dumps({"email": email, "name": name})


def get_session(request: Request) -> dict | None:
    session_cookie = request.cookies.get("session")
    if not session_cookie:
        return None
    try:
        data = serializer.loads(session_cookie, max_age=86400)  # 24 hour session
        return data
    except Exception:
        return None


def is_authenticated(request: Request) -> bool:
    if not AUTH_ENABLED:
        return True
    return get_session(request) is not None
