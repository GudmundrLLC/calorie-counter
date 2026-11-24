"""Gudmundr Gateway SSO (ecosystem single sign-on).

Flow:
  1. GET /login/gateway  -> 303 to {GATEWAY_URL}/auth/login
     ?redirect_uri=<public origin>/auth/gateway/callback&state=<csrf>
  2. The Gateway signs the user in (Google at the Gateway, or an existing
     Gateway session) and 302s back with ?token=<jwt>&state=<csrf>.
  3. GET /auth/gateway/callback checks the state, verifies the RS256 token
     locally against the Gateway JWKS ({GATEWAY_URL}/jwks.json · cached 5 min,
     refetched once on an unknown kid), falls back to {GATEWAY_URL}/auth/userinfo
     only when the JWKS cannot be fetched, and hands the email to the app's
     own allow-list before the session is populated. The callback then
     redirects to a clean URL so the token never stays in the address bar.

The redirect_uri is built from the host the browser is on (see
public_origin) so it matches the Gateway's allow-list (*.gudmundr.com or a
catalogued host) and shares an origin with the session cookie holding the
CSRF state.

Env:
  GATEWAY_URL          default https://gateway.gudmundr.com ('' disables)
  PUBLIC_URL           optional canonical origin, used when the Host header
                       is not trusted
  PUBLIC_HOST_ALIASES  optional comma list of extra trusted hosts
"""

from __future__ import annotations

import json as _json
import os
import secrets
import urllib.error as _urllib_error
import urllib.request as _urllib_request
from urllib.parse import urlencode, urlparse

GATEWAY_URL = os.environ.get("GATEWAY_URL", "https://gateway.gudmundr.com").rstrip("/")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


def is_gateway_configured() -> bool:
    return bool(GATEWAY_URL)


def _trusted_host(host: str, fallback_base: str) -> bool:
    host = (host or "").lower()
    if not host:
        return False
    if host in _LOCAL_HOSTS or host == "gudmundr.com" or host.endswith(".gudmundr.com"):
        return True
    extra = {h.strip().lower() for h in os.environ.get("PUBLIC_HOST_ALIASES", "").split(",") if h.strip()}
    if host in extra:
        return True
    return bool(fallback_base) and host == (urlparse(fallback_base).hostname or "").lower()


def public_origin(request, fallback_base: str = "") -> str:
    """https://<host the user is on> when trusted, else PUBLIC_URL / fallback."""
    fwd_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host_hdr = fwd_host or request.headers.get("host", "")
    hostname = host_hdr.split(":")[0].strip().lower()
    if not _trusted_host(hostname, fallback_base):
        return (os.environ.get("PUBLIC_URL") or fallback_base).rstrip("/")
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    if hostname not in _LOCAL_HOSTS:
        proto = "https"
    return f"{proto}://{host_hdr}".rstrip("/")


def gateway_callback_url(request, fallback_base: str = "", path: str = "/auth/gateway/callback") -> str:
    return public_origin(request, fallback_base) + path


def gateway_authorize_url(callback_url: str, state: str, app_id: str = "") -> str:
    """Access claim v1: `app` makes the Gateway refuse users without access."""
    q = {"redirect_uri": callback_url, "state": state}
    if app_id:
        q = {"app": app_id, **q}
    return f"{GATEWAY_URL}/auth/login?" + urlencode(q)


def new_oauth_state(request) -> str:
    s = secrets.token_urlsafe(24)
    request.session["gateway_oauth_state"] = s
    return s


def check_state(request, state: str) -> bool:
    expected = request.session.pop("gateway_oauth_state", None)
    return bool(expected) and bool(state) and secrets.compare_digest(str(expected), str(state))


_JWKS_TTL_S = 300
_jwks_cache: dict = {"at": 0.0, "keys": None}


def _fetch_jwks(force: bool = False):
    import time as _time
    from authlib.jose import JsonWebKey
    now = _time.time()
    if not force and _jwks_cache["keys"] is not None and now - _jwks_cache["at"] < _JWKS_TTL_S:
        return _jwks_cache["keys"]
    with _urllib_request.urlopen(f"{GATEWAY_URL}/jwks.json", timeout=8) as resp:
        body = _json.loads(resp.read().decode("utf-8"))
    keys = JsonWebKey.import_key_set(body)
    _jwks_cache.update(at=now, keys=keys)
    return keys


def verify_token(token: str) -> tuple[dict | None, str]:
    """Verify a Gateway RS256 JWT against the Gateway JWKS.

    Returns (claims, detail). detail is 'ok', a short failure reason, or
    'jwks_unavailable' (caller may then fall back to /auth/userinfo)."""
    import time as _time
    from authlib.jose import jwt as _jwt
    from authlib.jose.errors import JoseError
    if not token or token.count(".") != 2:
        return None, "malformed_token"
    try:
        keys = _fetch_jwks()
    except Exception:  # noqa: BLE001
        return None, "jwks_unavailable"
    claims = None
    for attempt in (0, 1):
        try:
            claims = _jwt.decode(token, keys, claims_options={"exp": {"essential": True}})
            break
        except (JoseError, ValueError) as e:
            # Unknown kid / rotated key: refetch once, then give up.
            if attempt == 0:
                try:
                    keys = _fetch_jwks(force=True)
                    continue
                except Exception:  # noqa: BLE001
                    return None, "jwks_unavailable"
            return None, f"invalid_token:{type(e).__name__}"
    if claims is None:
        return None, "invalid_token"
    if claims.header.get("alg") != "RS256":
        return None, "unexpected_alg"
    try:
        claims.validate(now=int(_time.time()), leeway=30)
    except JoseError as e:
        return None, f"invalid_claims:{type(e).__name__}"
    if not claims.get("sub"):
        return None, "no_subject"
    if not (claims.get("email") or "").strip():
        return None, "no_email"
    return dict(claims), "ok"


def verify_or_userinfo(token: str) -> tuple[dict | None, str]:
    """JWKS first; /auth/userinfo only when the JWKS itself is unreachable."""
    claims, detail = verify_token(token)
    if claims or detail != "jwks_unavailable":
        return claims, detail
    return fetch_userinfo(token)


def gateway_logout_url(redirect_uri: str) -> str:
    return f"{GATEWAY_URL}/auth/logout?" + urlencode({"redirect_uri": redirect_uri})


def fetch_userinfo(token: str) -> tuple[dict | None, str]:
    """Verify a Gateway token via /auth/userinfo. Returns (claims, detail)."""
    if not token:
        return None, "missing_token"
    req = _urllib_request.Request(
        f"{GATEWAY_URL}/auth/userinfo", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with _urllib_request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return None, f"userinfo_{resp.status}"
            body = _json.loads(resp.read().decode("utf-8"))
    except _urllib_error.HTTPError as e:
        return None, f"userinfo_{e.code}"
    except Exception as e:  # noqa: BLE001
        return None, f"userinfo_{type(e).__name__}"
    if not (body.get("email") or "").strip():
        return None, "no_email"
    return body, "ok"
