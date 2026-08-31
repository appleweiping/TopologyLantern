from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from topology_lantern.cli import EXIT_EMPTY, EXIT_INPUT, EXIT_OK, main
from topology_lantern.emit import (
    candidate_explanation,
    candidate_spice,
    result_json,
    result_text,
)
from topology_lantern.explain import explain_candidate
from topology_lantern.search import generate_candidates
from topology_lantern.spec import DesignSpec


def spec_mapping(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {"name": "cli", "supply_voltage": 1.8}
    value.update(updates)
    return value


def write_spec(tmp_path: Path, **updates: object) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec_mapping(**updates)), encoding="utf-8")
    return path


def test_result_json_is_stable_round_trippable_and_pretty() -> None:
    result = generate_candidates(spec_mapping(), limit=2)
    compact = result_json(result)
    pretty = result_json(result, pretty=True)
    assert compact.endswith("\n")
    assert pretty.endswith("\n")
    assert '\n  "' in pretty
    assert json.loads(compact) == result.as_dict()
    assert result_json(result) == compact


def test_text_summary_includes_search_and_candidate_paths() -> None:
    result = generate_candidates(spec_mapping(), limit=2)
    text = result_text(result)
    assert text.startswith("TopologyLantern: 2 candidates")
    assert "spec: sha256:" in text
    assert "search:" in text
    for candidate in result.candidates:
        assert candidate.candidate_id in text
        assert candidate.trace[0].rule_id in text


def test_explanation_contains_metrics_rationales_and_boundary() -> None:
    candidate = generate_candidates(spec_mapping(), limit=1).candidates[0]
    text = explain_candidate(candidate)
    assert candidate.candidate_id in text
    assert candidate.signature in text
    assert "metrics:" in text
    assert "derivation:" in text
    assert candidate.trace[0].rationale in text
    assert "conceptual topology only" in text
    assert candidate_explanation(candidate) == text


@pytest.mark.parametrize(
    "input_mode",
    ["single", "differential"],
)
def test_spice_skeleton_contains_every_device_and_no_analysis(input_mode: str) -> None:
    candidate = generate_candidates(spec_mapping(input_mode=input_mode), limit=1).candidates[0]
    text = candidate_spice(candidate)
    assert text.startswith("* TopologyLantern conceptual candidate")
    assert "UNSIZED REVIEW ARTIFACT" in text
    assert text.endswith(".end\n")
    commands = [line.lower() for line in text.splitlines() if line.startswith(".")]
    assert not any(line.startswith(".tran") for line in commands)
    assert not any(line.startswith(".ac") for line in commands)
    assert not any(line.startswith(".include") for line in commands)
    for device in candidate.topology.devices:
        assert any(line.startswith(device.name + " ") for line in text.splitlines())


def test_spice_skeleton_uses_explicit_placeholders_by_kind() -> None:
    candidate = generate_candidates(
        spec_mapping(require_compensation=True, load_preference="resistive"),
        limit=8,
    ).candidates[-1]
    text = candidate_spice(candidate)
    assert "NMOS_PLACEHOLDER" in text
    assert "{R_UNSIZED}" in text
    assert "{C_UNSIZED}" in text
    assert "{I_UNSIZED}" in text
    assert "Missing by design" in text


def test_cli_validate_spec_and_invalid_spec(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    stdout = io.StringIO()
    assert main(["validate-spec", str(path)], stdout=stdout) == EXIT_OK
    assert stdout.getvalue().startswith("valid specification: sha256:")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    errors = io.StringIO()
    assert main(["validate-spec", str(invalid)], stderr=errors) == EXIT_INPUT
    assert errors.getvalue().startswith("topology-lantern:")


def test_cli_generate_text_json_and_spice(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    text = io.StringIO()
    assert main(["generate", str(path), "--limit", "2"], stdout=text) == EXIT_OK
    assert "2 candidates" in text.getvalue()
    json_out = io.StringIO()
    assert (
        main(
            ["generate", str(path), "--limit", "2", "--format", "json", "--pretty"],
            stdout=json_out,
        )
        == EXIT_OK
    )
    assert len(json.loads(json_out.getvalue())["candidates"]) == 2
    spice = io.StringIO()
    assert (
        main(
            ["generate", str(path), "--limit", "2", "--format", "spice", "--candidate", "2"],
            stdout=spice,
        )
        == EXIT_OK
    )
    assert "UNSIZED REVIEW ARTIFACT" in spice.getvalue()


def test_cli_writes_output_file_without_stdout(tmp_path: Path) -> None:
    path = write_spec(tmp_path)
    destination = tmp_path / "report.json"
    stdout = io.StringIO()
    result = main(
        [
            "generate",
            str(path),
            "--limit",
            "2",
            "--format",
            "json",
            "--output",
            str(destination),
        ],
        stdout=stdout,
    )
    assert result == EXIT_OK
    assert stdout.getvalue() == ""
    assert len(json.loads(destination.read_text(encoding="utf-8"))["candidates"]) == 2


def generate_report(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    spec_path = write_spec(tmp_path)
    report_path = tmp_path / "report.json"
    assert (
        main(
            [
                "generate",
                str(spec_path),
                "--limit",
                "3",
                "--format",
                "json",
                "--output",
                str(report_path),
            ]
        )
        == EXIT_OK
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return spec_path, report_path, report


def test_cli_explain_and_replay_generated_candidate(tmp_path: Path) -> None:
    spec_path, report_path, report = generate_report(tmp_path)
    candidate_id = report["candidates"][0]["candidate_id"]
    explanation = io.StringIO()
    assert main(["explain", str(report_path), candidate_id], stdout=explanation) == EXIT_OK
    assert candidate_id in explanation.getvalue()
    assert explanation.getvalue().startswith("UNVERIFIED STORED REPORT EVIDENCE")
    assert "derivation:" in explanation.getvalue()
    replay = io.StringIO()
    assert (
        main(["replay", str(spec_path), str(report_path), candidate_id], stdout=replay) == EXIT_OK
    )
    assert replay.getvalue().startswith("replay core evidence verified:")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("topology", {"ports": [], "nets": [], "devices": []}),
        ("facts", []),
        ("metrics", {"device_count": -999}),
        (
            "violations",
            [{"code": "FAKE", "severity": "warning", "message": "invented", "subjects": []}],
        ),
    ],
)
def test_cli_replay_rejects_tampered_core_evidence(
    tmp_path: Path, field: str, replacement: object
) -> None:
    spec_path, report_path, report = generate_report(tmp_path)
    candidate = report["candidates"][0]
    candidate_id = candidate["candidate_id"]
    candidate[field] = replacement
    report_path.write_text(json.dumps(report), encoding="utf-8")
    errors = io.StringIO()
    assert (
        main(["replay", str(spec_path), str(report_path), candidate_id], stderr=errors)
        == EXIT_INPUT
    )
    assert field in errors.getvalue()


def test_cli_replay_rejects_candidate_id_not_bound_to_signature(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec_mapping()), encoding="utf-8")
    report = generate_candidates(str(spec_path), limit=1).as_dict()
    report["candidates"][0]["candidate_id"] = "TL-tampered"
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    errors = io.StringIO()

    assert (
        main(["replay", str(spec_path), str(report_path), "TL-tampered"], stderr=errors)
        == EXIT_INPUT
    )
    assert "candidate ID" in errors.getvalue()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda report: report["tool"].update(version="forged"), "regenerated"),
        (lambda report: report["search"].update(explored_states=999), "regenerated"),
        (lambda report: report.update(rule_catalog=["forged"]), "regenerated"),
        (lambda report: report["candidates"][0].update(pareto_rank=999), "regenerated"),
        (lambda report: report["candidates"][0].update(score=-999), "regenerated"),
    ],
)
def test_cli_replay_binds_every_declared_report_field(tmp_path: Path, mutate, message) -> None:
    spec_path = write_spec(tmp_path)
    report_path = tmp_path / "report.json"
    report = generate_candidates(str(spec_path), limit=2).as_dict()
    candidate_id = report["candidates"][0]["candidate_id"]
    mutate(report)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    errors = io.StringIO()
    assert (
        main(["replay", str(spec_path), str(report_path), candidate_id], stderr=errors)
        == EXIT_INPUT
    )
    assert message in errors.getvalue()


def test_cli_explain_escapes_untrusted_terminal_controls(tmp_path: Path) -> None:
    _, report_path, report = generate_report(tmp_path)
    candidate = report["candidates"][0]
    candidate_id = candidate["candidate_id"]
    candidate["trace"][0]["summary"] = "清屏\u001b[2J\nforged"
    candidate["trace"][0]["rationale"] = "line\r\nnext\u0085"
    candidate["violations"] = [
        {
            "code": "FAKE\u001b",
            "severity": "warning",
            "message": "message\nforged",
            "subjects": [],
        }
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    output = io.StringIO()

    assert main(["explain", str(report_path), candidate_id], stdout=output) == EXIT_OK
    rendered = output.getvalue()
    assert "清屏\\x1b[2J\\nforged" in rendered
    assert "line\\r\\nnext\\x85" in rendered
    assert "message\\nforged" in rendered
    assert "\u001b" not in rendered
    assert "\u0085" not in rendered


@pytest.mark.parametrize("field", ["summary", "rationale"])
def test_cli_explain_rejects_non_string_trace_text(tmp_path: Path, field: str) -> None:
    _, report_path, report = generate_report(tmp_path)
    candidate = report["candidates"][0]
    candidate_id = candidate["candidate_id"]
    candidate["trace"][0][field] = 7
    report_path.write_text(json.dumps(report), encoding="utf-8")
    errors = io.StringIO()

    assert main(["explain", str(report_path), candidate_id], stderr=errors) == EXIT_INPUT
    assert f"trace {field} must be a string" in errors.getvalue()


def test_cli_explain_rejects_non_string_violation_message(tmp_path: Path) -> None:
    _, report_path, report = generate_report(tmp_path)
    candidate = report["candidates"][0]
    candidate_id = candidate["candidate_id"]
    candidate["violations"] = [
        {"code": "FAKE", "severity": "warning", "message": 7, "subjects": []}
    ]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    errors = io.StringIO()

    assert main(["explain", str(report_path), candidate_id], stderr=errors) == EXIT_INPUT
    assert "violation message must be a string" in errors.getvalue()


def test_cli_replay_rejects_a_different_specification(tmp_path: Path) -> None:
    spec_path, report_path, report = generate_report(tmp_path)
    changed = json.loads(spec_path.read_text(encoding="utf-8"))
    changed["supply_voltage"] = 2.0
    spec_path.write_text(json.dumps(changed), encoding="utf-8")
    errors = io.StringIO()
    candidate_id = report["candidates"][0]["candidate_id"]
    assert (
        main(["replay", str(spec_path), str(report_path), candidate_id], stderr=errors)
        == EXIT_INPUT
    )
    assert "fingerprint does not match" in errors.getvalue()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"schema_version":2,"tool":{"name":"TopologyLantern"},"candidates":[]}',
        '{"schema_version":1,"tool":{"name":"Other"},"candidates":[]}',
        '{"schema_version":1,"tool":{"name":"TopologyLantern"},"candidates":{}}',
    ],
)
def test_cli_rejects_invalid_result_reports(tmp_path: Path, content: str) -> None:
    report = tmp_path / "bad.json"
    report.write_text(content, encoding="utf-8")
    errors = io.StringIO()
    assert main(["explain", str(report), "TL-nope"], stderr=errors) == EXIT_INPUT
    assert errors.getvalue().startswith("topology-lantern:")


@pytest.mark.parametrize(
    "content",
    [
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":1,"value":NaN}',
        "[" * 1_500 + "0" + "]" * 1_500,
    ],
)
def test_cli_report_loader_rejects_ambiguous_or_deep_json(tmp_path: Path, content: str) -> None:
    report = tmp_path / "hostile.json"
    report.write_text(content, encoding="utf-8")
    errors = io.StringIO()
    assert main(["explain", str(report), "TL-nope"], stderr=errors) == EXIT_INPUT
    assert errors.getvalue().startswith("topology-lantern:")


def test_cli_report_loader_rejects_oversized_json(tmp_path: Path) -> None:
    report = tmp_path / "oversized.json"
    report.write_bytes(b" " * 1_048_577)
    errors = io.StringIO()
    assert main(["explain", str(report), "TL-nope"], stderr=errors) == EXIT_INPUT
    assert "byte input limit" in errors.getvalue()


def test_cli_rejects_unknown_candidate_and_spice_index(tmp_path: Path) -> None:
    spec_path, report_path, _ = generate_report(tmp_path)
    errors = io.StringIO()
    assert main(["explain", str(report_path), "TL-missing"], stderr=errors) == EXIT_INPUT
    errors = io.StringIO()
    assert (
        main(
            ["generate", str(spec_path), "--limit", "1", "--format", "spice", "--candidate", "2"],
            stderr=errors,
        )
        == EXIT_INPUT
    )


def test_cli_reports_empty_search_with_exit_two(tmp_path: Path) -> None:
    path = write_spec(
        tmp_path,
        require_compensation=True,
        allowed_devices=["nmos", "pmos", "resistor", "current_source"],
    )
    stdout = io.StringIO()
    assert main(["generate", str(path)], stdout=stdout) == EXIT_EMPTY
    assert stdout.getvalue().startswith("TopologyLantern: 0 candidates")


def test_cli_rejects_tampered_trace_and_signature(tmp_path: Path) -> None:
    spec_path, report_path, report = generate_report(tmp_path)
    candidate = report["candidates"][0]
    candidate_id = candidate["candidate_id"]
    candidate["trace"][0]["rule_id"] = "output.direct"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    errors = io.StringIO()
    assert (
        main(["replay", str(spec_path), str(report_path), candidate_id], stderr=errors)
        == EXIT_INPUT
    )
    assert "not applicable" in errors.getvalue()


def test_cli_does_not_use_subprocess_network_or_random(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_spec(tmp_path)

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("external or random behavior is forbidden")

    monkeypatch.setattr("subprocess.Popen", fail)
    monkeypatch.setattr("socket.socket", fail)
    monkeypatch.setattr("random.random", fail)
    assert main(["generate", str(path), "--limit", "2"], stdout=io.StringIO()) == EXIT_OK


def test_report_candidate_shapes_are_complete() -> None:
    candidate = generate_candidates(spec_mapping(), limit=1).candidates[0]
    value = candidate.as_dict()
    assert value["candidate_id"] == candidate.candidate_id
    assert value["signature"] == candidate.signature
    assert value["topology"] == candidate.topology.as_dict()
    assert value["metrics"] == candidate.metrics.as_dict()
    assert value["trace"] == [step.as_dict() for step in candidate.trace]


def test_example_specs_load_and_generate() -> None:
    root = Path(__file__).parents[1]
    for name in ("low_voltage_diff_stage.json", "compensated_pmos_stage.json"):
        spec = DesignSpec.from_json(root / "examples" / name)
        result = generate_candidates(spec)
        assert result.candidates
        assert result.spec_fingerprint == spec.fingerprint()
