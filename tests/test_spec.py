from __future__ import annotations

import json
from pathlib import Path

import pytest

from topology_lantern.spec import DesignSpec, Objectives, SearchLimits, load_spec
from topology_lantern.types import DeviceKind, SpecError


def minimal(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {"name": "test stage", "supply_voltage": 1.8}
    value.update(updates)
    return value


def test_minimal_spec_uses_documented_defaults() -> None:
    spec = DesignSpec.from_mapping(minimal())
    assert spec.name == "test stage"
    assert spec.supply_voltage == 1.8
    assert spec.input_mode == "differential"
    assert spec.output_mode == "single"
    assert spec.polarity == "nmos_input"
    assert spec.load_preference == "either"
    assert spec.require_compensation is False
    assert spec.allow_resistive_bias is True
    assert spec.allowed_devices == tuple(DeviceKind)
    assert spec.limits == SearchLimits()
    assert spec.objectives == Objectives()


def test_full_spec_round_trip_is_canonical() -> None:
    original = minimal(
        schema_version=1,
        input_mode="differential",
        output_mode="differential",
        polarity="pmos_input",
        load_preference="active",
        require_compensation=True,
        allow_resistive_bias=False,
        allowed_devices=["pmos", "nmos", "capacitor", "current_source"],
        limits={
            "max_candidates": 3,
            "max_states": 100,
            "max_depth": 7,
            "max_devices": 18,
            "max_canonical_permutations": 720,
        },
        objectives={
            "device_count": 1,
            "headroom": 2,
            "symmetry": 3,
            "passives": 4,
            "warnings": 5,
        },
    )
    spec = DesignSpec.from_mapping(original)
    assert spec.as_dict() == original
    assert DesignSpec.from_mapping(spec.as_dict()) == spec
    assert len(spec.fingerprint()) == 64


def test_json_loader_and_load_spec_variants(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(minimal()), encoding="utf-8")
    from_file = DesignSpec.from_json(path)
    assert load_spec(path) == from_file
    assert load_spec(str(path)) == from_file
    assert load_spec(minimal()) == from_file
    assert load_spec(from_file) is from_file


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        {"name": "missing supply"},
        {"supply_voltage": 1.8},
        minimal(schema_version=2),
        minimal(extra=True),
        minimal(name=""),
        minimal(name="x" * 81),
        minimal(supply_voltage=0),
        minimal(supply_voltage=101),
        minimal(supply_voltage="not-number"),
        minimal(supply_voltage="1.8"),
        minimal(supply_voltage=True),
        minimal(name=123),
        minimal(input_mode="balanced"),
        minimal(output_mode="balanced"),
        minimal(input_mode="single", output_mode="differential"),
        minimal(polarity="bipolar"),
        minimal(load_preference="magic"),
        minimal(require_compensation=1),
        minimal(allow_resistive_bias="yes"),
        minimal(allowed_devices=[]),
        minimal(allowed_devices="nmos"),
        minimal(allowed_devices=["nmos", "nmos"]),
        minimal(allowed_devices=["pmos", "resistor"]),
        minimal(allowed_devices=["unknown"]),
        minimal(limits=[]),
        minimal(limits={"unknown": 1}),
        minimal(limits={"max_states": 0}),
        minimal(limits={"max_depth": "bad"}),
        minimal(limits={"max_depth": True}),
        minimal(limits={"max_depth": 2.5}),
        minimal(objectives=[]),
        minimal(objectives={"unknown": 1}),
        minimal(objectives={"headroom": -1}),
        minimal(objectives={"headroom": False}),
        minimal(objectives={"headroom": 1.5}),
        minimal(
            objectives={
                "device_count": 0,
                "headroom": 0,
                "symmetry": 0,
                "passives": 0,
                "warnings": 0,
            }
        ),
    ],
)
def test_invalid_specifications_are_rejected(mapping: dict[str, object]) -> None:
    with pytest.raises(SpecError):
        DesignSpec.from_mapping(mapping)


def test_json_loader_rejects_missing_invalid_and_non_object(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="cannot read"):
        DesignSpec.from_json(tmp_path / "missing.json")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(SpecError, match="invalid JSON"):
        DesignSpec.from_json(invalid)
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(SpecError, match="JSON object"):
        DesignSpec.from_json(array)


def test_json_loader_rejects_duplicate_nonfinite_deep_and_oversized_input(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"name":"one","name":"two","supply_voltage":1.8}', encoding="utf-8")
    with pytest.raises(SpecError, match="duplicate JSON key"):
        DesignSpec.from_json(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"name":"stage","supply_voltage":1e999}', encoding="utf-8")
    with pytest.raises(SpecError, match="non-finite"):
        DesignSpec.from_json(nonfinite)

    deep = tmp_path / "deep.json"
    deep.write_text("[" * 1_500 + "0" + "]" * 1_500, encoding="utf-8")
    with pytest.raises(SpecError, match=r"invalid JSON|complexity limits"):
        DesignSpec.from_json(deep)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 1_048_577)
    with pytest.raises(SpecError, match="byte input limit"):
        DesignSpec.from_json(oversized)


def test_input_is_not_mutated() -> None:
    value = minimal(limits={"max_candidates": 2}, allowed_devices=["nmos"])
    before = json.loads(json.dumps(value))
    DesignSpec.from_mapping(value)
    assert value == before


def test_field_case_is_normalized_but_name_is_preserved() -> None:
    spec = DesignSpec.from_mapping(
        minimal(
            name="My Stage",
            input_mode="DIFFERENTIAL",
            output_mode="SINGLE",
            polarity="NMOS_INPUT",
            load_preference="ACTIVE",
            allowed_devices=["NMOS", "PMOS", "CURRENT_SOURCE"],
        )
    )
    assert spec.name == "My Stage"
    assert spec.input_mode == "differential"
    assert spec.load_preference == "active"


def test_fingerprint_changes_for_design_intent_not_mapping_order() -> None:
    one = DesignSpec.from_mapping(minimal())
    two = DesignSpec.from_mapping({"supply_voltage": 1.8, "name": "test stage"})
    changed = DesignSpec.from_mapping(minimal(supply_voltage=1.2))
    assert one.fingerprint() == two.fingerprint()
    assert one.fingerprint() != changed.fingerprint()


def test_mapping_rejects_non_string_field_names_and_control_characters() -> None:
    with pytest.raises(SpecError, match="field names must be strings"):
        DesignSpec.from_mapping({"name": "stage", "supply_voltage": 1.8, 1: "bad"})
    with pytest.raises(SpecError, match="visible characters"):
        DesignSpec.from_mapping(minimal(name="stage\nspoofed"))
