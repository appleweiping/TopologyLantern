"""Strict design-intent schema and canonical fingerprinting."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from topology_lantern.types import DeviceKind, SpecError


@dataclass(frozen=True, slots=True)
class SearchLimits:
    max_candidates: int = 12
    max_states: int = 5000
    max_depth: int = 8
    max_devices: int = 24
    max_canonical_permutations: int = 40_320


@dataclass(frozen=True, slots=True)
class Objectives:
    device_count: int = 4
    headroom: int = 3
    symmetry: int = 2
    passives: int = 1
    warnings: int = 5


@dataclass(frozen=True, slots=True)
class DesignSpec:
    name: str
    supply_voltage: float
    input_mode: str = "differential"
    output_mode: str = "single"
    polarity: str = "nmos_input"
    load_preference: str = "either"
    require_compensation: bool = False
    allow_resistive_bias: bool = True
    allowed_devices: tuple[DeviceKind, ...] = field(default_factory=lambda: tuple(DeviceKind))
    limits: SearchLimits = field(default_factory=SearchLimits)
    objectives: Objectives = field(default_factory=Objectives)

    @classmethod
    def from_json(cls, path: str | Path) -> DesignSpec:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SpecError(f"cannot read specification {path}: {exc}") from exc
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecError(f"specification is invalid JSON: {exc.msg}") from exc
        if not isinstance(value, Mapping):
            raise SpecError("specification must contain a JSON object")
        return cls.from_mapping(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DesignSpec:
        allowed = {
            "schema_version",
            "name",
            "supply_voltage",
            "input_mode",
            "output_mode",
            "polarity",
            "load_preference",
            "require_compensation",
            "allow_resistive_bias",
            "allowed_devices",
            "limits",
            "objectives",
        }
        _reject_unknown(value, allowed, "specification")
        schema_version = value.get("schema_version", 1)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
        ):
            raise SpecError("schema_version must be 1")
        if "name" not in value or "supply_voltage" not in value:
            raise SpecError("name and supply_voltage are required")
        limits_value = _mapping(value.get("limits", {}), "limits")
        objectives_value = _mapping(value.get("objectives", {}), "objectives")
        _reject_unknown(limits_value, set(SearchLimits.__dataclass_fields__), "limits")
        _reject_unknown(objectives_value, set(Objectives.__dataclass_fields__), "objectives")
        try:
            limits = SearchLimits(
                **{key: _integer(item, f"limits.{key}") for key, item in limits_value.items()}
            )
            objectives = Objectives(
                **{
                    key: _integer(item, f"objectives.{key}")
                    for key, item in objectives_value.items()
                }
            )
            raw_devices = value.get("allowed_devices", [item.value for item in DeviceKind])
            if not isinstance(raw_devices, list) or not raw_devices:
                raise SpecError("allowed_devices must be a non-empty array")
            devices = tuple(
                DeviceKind(_text(item, "allowed_devices item").lower()) for item in raw_devices
            )
            spec = cls(
                name=_text(value["name"], "name"),
                supply_voltage=_number(value["supply_voltage"], "supply_voltage"),
                input_mode=_text(value.get("input_mode", "differential"), "input_mode").lower(),
                output_mode=_text(value.get("output_mode", "single"), "output_mode").lower(),
                polarity=_text(value.get("polarity", "nmos_input"), "polarity").lower(),
                load_preference=_text(
                    value.get("load_preference", "either"), "load_preference"
                ).lower(),
                require_compensation=_strict_bool(
                    value.get("require_compensation", False), "require_compensation"
                ),
                allow_resistive_bias=_strict_bool(
                    value.get("allow_resistive_bias", True), "allow_resistive_bias"
                ),
                allowed_devices=devices,
                limits=limits,
                objectives=objectives,
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, SpecError):
                raise
            raise SpecError(f"invalid specification value: {exc}") from exc
        spec.validate()
        return spec

    def validate(self) -> None:
        if (
            not self.name.strip()
            or len(self.name) > 80
            or any(not character.isprintable() for character in self.name)
        ):
            raise SpecError("name must contain 1 to 80 visible characters")
        if not 0.1 <= self.supply_voltage <= 100.0:
            raise SpecError("supply_voltage must be between 0.1 and 100 V")
        if self.input_mode not in {"single", "differential"}:
            raise SpecError("input_mode must be single or differential")
        if self.output_mode not in {"single", "differential"}:
            raise SpecError("output_mode must be single or differential")
        if self.output_mode == "differential" and self.input_mode != "differential":
            raise SpecError("differential output currently requires differential input intent")
        if self.polarity not in {"nmos_input", "pmos_input"}:
            raise SpecError("polarity must be nmos_input or pmos_input")
        if self.load_preference not in {"active", "resistive", "either"}:
            raise SpecError("load_preference must be active, resistive, or either")
        if len(set(self.allowed_devices)) != len(self.allowed_devices):
            raise SpecError("allowed_devices must not contain duplicates")
        for name in SearchLimits.__dataclass_fields__:
            if getattr(self.limits, name) <= 0:
                raise SpecError(f"limits.{name} must be positive")
        for name in Objectives.__dataclass_fields__:
            if getattr(self.objectives, name) < 0:
                raise SpecError(f"objectives.{name} must be non-negative")
        if not any(getattr(self.objectives, name) for name in Objectives.__dataclass_fields__):
            raise SpecError("at least one objective weight must be non-zero")
        required = {DeviceKind.NMOS if self.polarity == "nmos_input" else DeviceKind.PMOS}
        if not required.issubset(self.allowed_devices):
            raise SpecError("allowed_devices excludes the selected input transistor family")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "name": self.name,
            "supply_voltage": self.supply_voltage,
            "input_mode": self.input_mode,
            "output_mode": self.output_mode,
            "polarity": self.polarity,
            "load_preference": self.load_preference,
            "require_compensation": self.require_compensation,
            "allow_resistive_bias": self.allow_resistive_bias,
            "allowed_devices": [item.value for item in self.allowed_devices],
            "limits": {
                name: getattr(self.limits, name) for name in SearchLimits.__dataclass_fields__
            },
            "objectives": {
                name: getattr(self.objectives, name) for name in Objectives.__dataclass_fields__
            },
        }

    def fingerprint(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode()
        return sha256(payload).hexdigest()


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecError(f"{context} must be an object")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise SpecError(f"{context} field names must be strings")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SpecError(f"unknown {context} fields: {', '.join(unknown)}")


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise SpecError(f"{name} must be true or false")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise SpecError(f"{name} must be a string")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SpecError(f"{name} must be numeric")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise SpecError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise SpecError(f"{name} must be finite")
    return result


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{name} must be an integer")
    return value


def load_spec(spec: DesignSpec | Mapping[str, Any] | str | Path) -> DesignSpec:
    if isinstance(spec, DesignSpec):
        return spec
    if isinstance(spec, Mapping):
        return DesignSpec.from_mapping(spec)
    return DesignSpec.from_json(spec)
