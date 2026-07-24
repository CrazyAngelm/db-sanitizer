"""HMAC helpers for privacy-preserving mapping keys."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from db_sanitizer.errors import SanitizerError


class HMACKeyError(SanitizerError):
    """An HMAC key or key input is invalid."""


@dataclass(frozen=True, slots=True)
class HMACKey:
    """A validated key that derives group-scoped SHA-256 identifiers."""

    secret: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.secret, bytes) or len(self.secret) < 32:
            raise HMACKeyError("HMAC key must contain at least 32 bytes")

    @property
    def fingerprint(self) -> str:
        """Return a non-secret fingerprint suitable for compatibility checks."""
        return hashlib.sha256(self.secret).hexdigest()

    def digest(self, group_id: str, normalized_value: str) -> str:
        return source_hmac(self.secret, group_id, normalized_value)


def source_hmac(secret: bytes, group_id: str, normalized_value: str) -> str:
    """Return HMAC-SHA256(secret, UTF-8(group + NUL + normalized value))."""
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise HMACKeyError("HMAC key must contain at least 32 bytes")
    if not isinstance(group_id, str) or not group_id:
        raise HMACKeyError("mapping group identifier must be a non-empty string")
    if not isinstance(normalized_value, str):
        raise HMACKeyError("normalized mapping value must be a string")
    payload = f"{group_id}\x00{normalized_value}".encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()
