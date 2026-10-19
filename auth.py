"""Single-user auth: scrypt password hash in .env, signed session cookie, CSRF token, login throttling."""
from __future__ import annotations
import hashlib, hmac, os, secrets, time
from collections import defaultdict
from . import config

def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt:{salt.hex()}:{h.hex()}"

def verify_password(pw: str, stored: str | None = None) -> bool:
    stored = stored or config.APP_PASSWORD_HASH
    try:
        _, salt, h = stored.split(":")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False

_fails: dict[str, list[float]] = defaultdict(list)
def throttled(ip: str) -> bool:
    _fails[ip] = [t for t in _fails[ip] if time.time() - t < 300]
    return len(_fails[ip]) >= 5
def record_fail(ip: str): _fails[ip].append(time.time())

def csrf_token(session) -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]
def csrf_ok(session, token: str) -> bool:
    return bool(token) and hmac.compare_digest(session.get("csrf", ""), token)
