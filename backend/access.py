"""Who may use the public demo, and how much it may spend.

The landing page is public. The app, its data, its photos and its sockets need a
demo sign-in when DEMO_ACCESS_CODE is set. It is one shared code for the demo, not
user accounts: a plant would use its own single sign-on, per person.

Spending is limited in code, because the link is public and every check is a paid
call. A total budget and an hourly cap stop new paid work, whoever is asking.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

from . import config, db

log = logging.getLogger("ustad.access")

COOKIE = "ustad_demo"
_UNREADABLE_GRACE_S = 60.0
_last_read_ok = time.monotonic()
TTL_S = 7 * 24 * 3600
_SPEND_CACHE_S = 5.0
_spend_state: tuple[float, str | None] = (0.0, None)


def enabled() -> bool:
    return bool(config.DEMO_ACCESS_CODE)


def _key() -> bytes:
    # Changing the code signs everyone out, which is what you want after sharing it.
    return hashlib.sha256((config.DEMO_ACCESS_CODE + "|" + config.supervisor_token()).encode()).digest()


def _sign(expires: int) -> str:
    return hmac.new(_key(), str(expires).encode(), hashlib.sha256).hexdigest()


def issue() -> str:
    expires = int(time.time()) + TTL_S
    return f"{expires}.{_sign(expires)}"


def code_ok(code: object) -> bool:
    if not enabled() or not isinstance(code, str):
        return False
    return hmac.compare_digest(code.strip().upper().encode(), config.DEMO_ACCESS_CODE.strip().upper().encode())


def signed_in(token: str | None) -> bool:
    """Open when no code is set (local work and tests), else a valid unexpired cookie."""
    if not enabled():
        return True
    if not token or "." not in token:
        return False
    head, _, sig = token.partition(".")
    if not head.isdigit() or int(head) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(int(head)))


def spend_stop() -> str | None:
    """Why new paid work must stop, or None. Short cache: this runs before every paid call."""
    global _spend_state, _last_read_ok
    now = time.monotonic()
    if _spend_state[0] and now - _spend_state[0] < _SPEND_CACHE_S:
        return _spend_state[1]
    reason = None
    try:
        if db.usage_total() >= config.BUDGET_USD:
            reason = "The demo has reached its total spending limit."
        elif config.HOURLY_SPEND_CAP_USD and db.usage_last_hour() >= config.HOURLY_SPEND_CAP_USD:
            reason = "The demo is busy and has reached its hourly spending limit. Please try again later."
        _last_read_ok = now
    except Exception as exc:  # noqa: BLE001
        # A brief hiccup keeps the last decision. If spend stays unreadable, the
        # public link stops paid work rather than spend blind.
        log.warning("could not read spend: %s", type(exc).__name__)
        reason = _spend_state[1]
        if enabled() and now - _last_read_ok > _UNREADABLE_GRACE_S:
            reason = "The demo can't check its spending right now. Please try again later."
    _spend_state = (now, reason)
    return reason


def reset_spend_cache() -> None:
    global _spend_state, _last_read_ok
    _spend_state = (0.0, None)
    _last_read_ok = time.monotonic()
