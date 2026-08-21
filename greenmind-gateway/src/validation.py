"""Validation helpers shared by local gateway input and storage boundaries."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.config import settings

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")
_PAIRING_CODE_RE = re.compile(r"^[A-Za-z0-9-]{4,64}$")


def canonical_mac(value: str) -> str:
    """Return an uppercase colon-delimited MAC, rejecting unsafe identities."""
    if not isinstance(value, str):
        raise ValueError("MAC address must be text")
    compact = value.strip().replace(":", "").replace("-", "")
    if not _MAC_RE.fullmatch(compact):
        raise ValueError("invalid MAC address")
    compact = compact.upper()
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


class SensorReading(BaseModel):
    """One validated millivolt reading from the deployed sensor protocol."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    kind: str
    value: float
    unit: str

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value != "bio_signal":
            raise ValueError("kind must be 'bio_signal'")
        return value

    @field_validator("unit")
    @classmethod
    def _validate_unit(cls, value: str) -> str:
        if value != "mV":
            raise ValueError("unit must be 'mV'")
        return value

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: float) -> float:
        if not settings.sensor_value_min_mv <= value <= settings.sensor_value_max_mv:
            raise ValueError("reading value is outside the configured millivolt range")
        return value


class SensorBatch(BaseModel):
    """Strict on-wire schema shared by both local ingest endpoints."""

    model_config = ConfigDict(extra="forbid", strict=True)

    mac_address: str
    sample_rate: int
    readings: list[SensorReading] = Field(min_length=1)

    @field_validator("mac_address", mode="before")
    @classmethod
    def _canonicalize_mac(cls, value: object) -> str:
        return canonical_mac(value)  # type: ignore[arg-type]

    @field_validator("sample_rate")
    @classmethod
    def _validate_sample_rate(cls, value: int) -> int:
        if value not in settings.allowed_sample_rates:
            allowed = ", ".join(str(rate) for rate in settings.allowed_sample_rates)
            raise ValueError(f"sample_rate must be one of: {allowed}")
        return value

    @model_validator(mode="after")
    def _validate_reading_count(self) -> "SensorBatch":
        if len(self.readings) > settings.max_samples_per_batch:
            raise ValueError(f"readings exceeds maximum of {settings.max_samples_per_batch}")
        return self


class SensorRegistration(BaseModel):
    """Validated local-to-cloud sensor pairing request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    mac_address: str
    code: str

    @field_validator("mac_address", mode="before")
    @classmethod
    def _canonicalize_mac(cls, value: object) -> str:
        return canonical_mac(value)  # type: ignore[arg-type]

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        value = value.strip()
        if not _PAIRING_CODE_RE.fullmatch(value):
            raise ValueError("invalid pairing code")
        return value
