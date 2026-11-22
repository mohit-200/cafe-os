"""Signed tokens. Customers stay anonymous: owning the visit token proves you own the visit."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


def _sign(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def visit_token(secret: str, cafe_id: str, visit_id: str) -> str:
    return _sign(secret, f"visit:{cafe_id}:{visit_id}")


def check_visit_token(secret: str, cafe_id: str, visit_id: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(visit_token(secret, cafe_id, visit_id), token)


def staff_token(secret: str, cafe_id: str, ttl_sec: int = 12 * 3600) -> str:
    exp = int(time.time()) + ttl_sec
    return f"{exp}.{_sign(secret, f'staff:{cafe_id}:{exp}')}"


def check_staff_token(secret: str, cafe_id: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, sig = token.split(".", 1)
    if not exp_s.isdigit() or int(exp_s) < time.time():
        return False
    return hmac.compare_digest(_sign(secret, f"staff:{cafe_id}:{exp_s}"), sig)
