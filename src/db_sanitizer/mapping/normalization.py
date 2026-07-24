"""Deterministic normalization shared by mapping collection and lookup."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from db_sanitizer.errors import SanitizerError

NormalizationKind = Literal["human_text", "email", "phone"]


class NormalizationError(SanitizerError):
    """A value cannot safely be used as a mapping key."""


_WHITESPACE = re.compile(r"\s+")


def normalize(value: str, kind: NormalizationKind, *, allow_empty: bool = True) -> str:
    """Return the canonical representation for a non-NULL source value.

    NULLs are intentionally not accepted: callers must preserve them rather than
    accidentally creating a mapping for them.
    """
    if not isinstance(value, str):
        raise NormalizationError("mapping value must be a non-NULL string")

    normalized = unicodedata.normalize("NFKC", value).strip()
    if kind == "human_text":
        normalized = _WHITESPACE.sub(" ", normalized).casefold()
    elif kind == "email":
        normalized = normalized.casefold()
    elif kind == "phone":
        normalized = "".join(character for character in normalized if character.isdigit())
    else:
        raise NormalizationError("unsupported mapping normalization strategy")

    if not normalized and not allow_empty:
        raise NormalizationError("empty mapping values are not permitted")
    return normalized
