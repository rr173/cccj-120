"""Parameter validation helpers.

Nothing in this package ever reads the machine clock: every instant is
supplied by the caller and must carry timezone information.
"""

from __future__ import annotations

from datetime import datetime

from .errors import InvalidParameterError


def require_aware_datetime(value: datetime, name: str) -> datetime:
    """Require a timezone-aware ``datetime`` (contract: no naive instants)."""
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidParameterError(
            f"{name} must carry timezone information, got {value!r}")
    return value


def require_product_token(token: str) -> str:
    """Require a single non-empty ASCII product token (no whitespace)."""
    if not isinstance(token, str):
        raise TypeError(f"product token must be str, got {type(token).__name__}")
    if not token or not token.isascii() or any(ch.isspace() for ch in token):
        raise InvalidParameterError(
            f"product token must be a single non-empty ASCII token, got {token!r}")
    return token


def parse_iso_instant(text: str, name: str = "instant") -> datetime:
    """Parse an ISO-8601 instant that must carry a timezone offset."""
    if not isinstance(text, str):
        raise TypeError(f"{name} must be str, got {type(text).__name__}")
    raw = text.strip()
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidParameterError(
            f"{name} is not a valid ISO-8601 instant: {text!r}") from exc
    return require_aware_datetime(value, name)
