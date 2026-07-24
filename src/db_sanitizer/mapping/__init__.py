"""Opaque, consistent mapping primitives."""

from .hmac_key import HMACKey, HMACKeyError, source_hmac
from .normalization import NormalizationError, normalize
from .registry import (
    MappingRecord,
    MappingRegistry,
    RegistryCompatibilityError,
    RegistryConflictError,
    RegistryError,
    RunMetadata,
)

__all__ = [
    "HMACKey",
    "HMACKeyError",
    "MappingRecord",
    "MappingRegistry",
    "NormalizationError",
    "RegistryCompatibilityError",
    "RegistryConflictError",
    "RegistryError",
    "RunMetadata",
    "normalize",
    "source_hmac",
]
