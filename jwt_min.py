"""Minimal HS256 JWT verify, byte-for-byte compatible with reefy-proxy's
verify_jwt / the backend's _create_service_token (header.payload.sig,
base64url, HMAC-SHA256 over "header.payload"). No external deps so the
container stays tiny and the format never drifts from the proxy.
"""
import base64
import hashlib
import hmac
import json
import time


def _b64url_decode(s: str) -> bytes:
    pad = '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def verify_jwt(token: str, secret: str) -> dict | None:
    """Return the decoded payload if the signature is valid and (when
    present) not expired; else None."""
    if not token or not secret:
        return None
    parts = token.split('.')
    if len(parts) != 3:
        return None
    try:
        expected = hmac.new(
            secret.encode(), f'{parts[0]}.{parts[1]}'.encode(),
            hashlib.sha256).digest()
        actual = _b64url_decode(parts[2])
        if not hmac.compare_digest(expected, actual):
            return None
        payload = json.loads(_b64url_decode(parts[1]))
        exp = payload.get('exp')
        if exp is not None and exp < time.time():
            return None
        return payload
    except Exception:
        return None
