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


class QualityCounts(BaseModel):
    """Bounded per-batch signal-quality counters from the sensor."""

    model_config = ConfigDict(extra="forbid", strict=True)

    valid: int = Field(ge=0)
    lead_off: int = Field(ge=0)
    rail_high: int = Field(ge=0)
    rail_low: int = Field(ge=0)
    jump: int = Field(ge=0)
    recovery: int = Field(ge=0)


class SensorBatch(BaseModel):
    """Strict on-wire schema shared by both local ingest endpoints."""

    model_config = ConfigDict(extra="forbid", strict=True)

    mac_address: str
    sample_rate: int
    protocol_version: int = Field(1, ge=1, le=100)
    firmware_version: str | None = Field(None, min_length=1, max_length=50)
    calibration_version: str | None = Field(None, min_length=1, max_length=50)
    boot_id: int | None = Field(None, ge=0, le=4_294_967_295)
    sequence: int | None = Field(None, ge=0, le=4_294_967_295)
    uptime_ms: int | None = Field(None, ge=0, le=4_294_967_295)
    dropped_samples_total: int | None = Field(None, ge=0, le=4_294_967_295)
    captured_at_epoch_ms: int | None = Field(
        None,
        ge=1_577_836_800_000,
        le=4_102_444_800_000,
    )
    quality_counts: QualityCounts | None = None
    readings: list[SensorReading] | None = None
    kind: str | None = None
    unit: str | None = None
    value_scale_mv: float | None = Field(None, gt=0, le=10)
    values_deci_mv: list[int] | None = None

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
        has_readings = self.readings is not None
        has_compact = self.values_deci_mv is not None
        if has_readings == has_compact:
            raise ValueError("exactly one sample representation is required")
        if has_compact:
            if self.protocol_version < 3:
                raise ValueError("compact samples require protocol_version 3")
            if self.kind != "bio_signal" or self.unit != "mV":
                raise ValueError("compact samples require bio_signal millivolts")
            if self.value_scale_mv != 0.1:
                raise ValueError("compact samples require value_scale_mv 0.1")
            if not self.values_deci_mv:
                raise ValueError("compact samples cannot be empty")
            if any(value < 0 or value > 65_535 for value in self.values_deci_mv):
                raise ValueError("compact sample is outside uint16 range")
            if any(
                not settings.sensor_value_min_mv
                <= value * self.value_scale_mv
                <= settings.sensor_value_max_mv
                for value in self.values_deci_mv
            ):
                raise ValueError("compact sample is outside configured millivolt range")

        sample_count = len(self.readings or self.values_deci_mv or [])
        if sample_count < 1:
            raise ValueError("sample representation cannot be empty")
        if sample_count > settings.max_samples_per_batch:
            raise ValueError(f"readings exceeds maximum of {settings.max_samples_per_batch}")
        if self.quality_counts is not None:
            if any(
                count > sample_count
                for count in (
                    self.quality_counts.valid,
                    self.quality_counts.lead_off,
                    self.quality_counts.rail_high,
                    self.quality_counts.rail_low,
                    self.quality_counts.jump,
                    self.quality_counts.recovery,
                )
            ):
                raise ValueError("quality counter exceeds reading count")
        return self

    def decoded_values_mv(self) -> list[float]:
        if self.readings is not None:
            return [reading.value for reading in self.readings]
        return [value * 0.1 for value in self.values_deci_mv or []]


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
