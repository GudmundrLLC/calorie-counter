"""Gudmundr Gateway SSO (ecosystem single sign-on).

Flow:
  1. GET /login/gateway  -> 303 to {GATEWAY_URL}/auth/login
     ?redirect_uri=<public origin>/auth/gateway/callback&state=<csrf>
  2. The Gateway signs the user in (Google at the Gateway, or an existing
     Gateway session) and 302s back with ?token=<jwt>&state=<csrf>.
  3. GET /auth/gateway/callback checks the state, verifies the token by
     calling {GATEWAY_URL}/auth/userinfo with it, and hands the email to the
     app's own allow-list before the session is populated.

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


def gateway_authorize_url(callback_url: str, state: str) -> str:
    return f"{GATEWAY_URL}/auth/login?" + urlencode({"redirect_uri": callback_url, "state": state})


def new_oauth_state(request) -> str:
    s = secrets.token_urlsafe(24)
    request.session["gateway_oauth_state"] = s
    return s


def check_state(request, state: str) -> bool:
    expected = request.session.pop("gateway_oauth_state", None)
    return bool(expected) and bool(state) and secrets.compare_digest(str(expected), str(state))


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
