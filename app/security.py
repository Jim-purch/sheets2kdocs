"""Session auth: signed cookie + constant-time credential check + login throttle."""
from __future__ import annotations

import hmac
import secrets
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

SESSION_COOKIE = "s2k_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days

_serializer: URLSafeTimedSerializer | None = None

# naive in-memory login throttle: ip -> (fail_count, lock_until)
_attempts: dict[str, tuple[int, float]] = {}
MAX_FAILURES = 5
LOCK_SECONDS = 300


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(settings.secret_key, salt="s2k-session")
    return _serializer


def make_session(username: str) -> str:
    return _get_serializer().dumps({"u": username})


def read_session(token: str | None) -> str | None:
    if not token:
        return None
    try:
        data = _get_serializer().loads(token, max_age=SESSION_MAX_AGE)
        return str(data.get("u") or "") or None
    except (BadSignature, SignatureExpired):
        return None


def verify_login(username: str, password: str) -> bool:
    ok_u = hmac.compare_digest(username.strip(), settings.admin_username)
    ok_p = hmac.compare_digest(password, settings.admin_password)
    return ok_u and ok_p


def client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_locked(ip: str) -> bool:
    count, until = _attempts.get(ip, (0, 0.0))
    if until > time.time():
        return True
    if until and until <= time.time():
        _attempts.pop(ip, None)
    return False


def register_failure(ip: str) -> None:
    count, until = _attempts.get(ip, (0, 0.0))
    count += 1
    until = time.time() + LOCK_SECONDS if count >= MAX_FAILURES else 0.0
    _attempts[ip] = (count, until)


def register_success(ip: str) -> None:
    _attempts.pop(ip, None)


def new_state() -> str:
    return secrets.token_urlsafe(16)
