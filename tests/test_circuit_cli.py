from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from topology_lantern.cli import EXIT_INPUT, EXIT_OK, main


def test_connectivity_graph_cli_emits_both_lossless_views(tmp_path: Path) -> None:
    netlist = tmp_path / "hierarchy.sp"
    netlist.write_text(
        ".subckt leaf p n\nr1 p n 1k\n.ends\n.subckt top p n\nx1 p n leaf\nc1 p n 1p\n.ends\n",
        encoding="utf-8",
    )
    compact_output = io.StringIO()
    assert (
        main(
            ["connectivity-graph", str(netlist), "--top", "top", "--view", "compact"],
            stdout=compact_output,
        )
        == EXIT_OK
    )
    compact = json.loads(compact_output.getvalue())
    assert compact["view"] == "compact"
    assert compact["source_graph_id"].startswith("sha256:")

    pin_path = tmp_path / "pin.json"
    assert (
        main(
            [
                "connectivity-graph",
                str(netlist),
                "--top",
                "top",
                "--view",
                "pin-level",
                "--pretty",
                "--output",
                str(pin_path),
            ]
        )
        == EXIT_OK
    )
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    assert pin["view"] == "pin-level"
    assert sum(len(scope["pins"]) for scope in pin["scopes"]) == sum(
        len(scope["links"]) for scope in pin["scopes"]
    )

    transcoded = tmp_path / "compact.json"
    assert (
        main(
            [
                "transcode-connectivity",
                str(pin_path),
                "--view",
                "compact",
                "--output",
                str(transcoded),
            ]
        )
        == EXIT_OK
    )
    converted = json.loads(transcoded.read_text(encoding="utf-8"))
    assert converted["view"] == "compact"
    assert converted["source_graph_id"] == compact["source_graph_id"]

    original_pin = pin_path.read_bytes()
    errors = io.StringIO()
    assert (
        main(
            [
                "transcode-connectivity",
                str(pin_path),
                "--view",
                "compact",
                "--output",
                str(pin_path),
                "--force",
            ],
            stderr=errors,
        )
        == EXIT_INPUT
    )
    assert "aliases protected input" in errors.getvalue()
    assert pin_path.read_bytes() == original_pin


def test_ingest_and_layout_evidence_cli(tmp_path: Path) -> None:
    netlist = tmp_path / "mirror.sp"
    netlist.write_text(
        ".subckt mirror ref out vdd\nmref ref ref vdd vdd pch\nmout out ref vdd vdd pch\n.ends\n",
        encoding="utf-8",
    )
    graph_output = io.StringIO()
    assert (
        main(
            ["ingest-spice", str(netlist), "--top", "mirror", "--pretty"],
            stdout=graph_output,
        )
        == EXIT_OK
    )
    graph = json.loads(graph_output.getvalue())
    devices = [device["id"] for device in graph["scopes"][0]["devices"]]

    constraints = tmp_path / "constraints.json"
    constraints.write_text(
        json.dumps(
            {
                "schema": "org.topology-lantern.layout-constraints",
                "version": 1,
                "graph_id": graph["graph_id"],
                "constraints": [
                    {
                        "id": "declared-match",
                        "kind": "matching",
                        "subjects": devices,
                        "tolerance": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"
    stdout = io.StringIO()
    assert (
        main(
            [
                "layout-evidence",
                str(netlist),
                "--constraints",
                str(constraints),
                "--output",
                str(report_path),
            ],
            stdout=stdout,
        )
        == EXIT_OK
    )
    assert stdout.getvalue() == ""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["user_constraints"][0]["origin"] == "user"
    assert report["inferred_constraints"][0]["origin"] == "inferred"

    original_constraints = constraints.read_text(encoding="utf-8")
    errors = io.StringIO()
    assert (
        main(
            [
                "layout-evidence",
                str(netlist),
                "--constraints",
                str(constraints),
                "--output",
                str(constraints),
                "--force",
            ],
            stderr=errors,
        )
        == EXIT_INPUT
    )
    assert "aliases protected input" in errors.getvalue()
    assert constraints.read_text(encoding="utf-8") == original_constraints


def test_circuit_cli_reports_input_error() -> None:
    errors = io.StringIO()
    assert main(["ingest-spice", "missing.sp"], stderr=errors) == EXIT_INPUT
    assert errors.getvalue().startswith("topology-lantern:")


def test_output_is_atomic_no_clobber_and_force_is_explicit(tmp_path: Path) -> None:
    netlist = tmp_path / "circuit.sp"
    original = "r1 a b 1k\n"
    netlist.write_text(original, encoding="utf-8")
    destination = tmp_path / "graph.json"
    destination.write_text("sentinel", encoding="utf-8")

    errors = io.StringIO()
    assert (
        main(
            ["ingest-spice", str(netlist), "--output", str(destination)],
            stderr=errors,
        )
        == EXIT_INPUT
    )
    assert "already exists" in errors.getvalue()
    assert destination.read_text(encoding="utf-8") == "sentinel"
    assert list(tmp_path.glob(".topology-lantern-*.tmp")) == []

    assert main(["ingest-spice", str(netlist), "--output", str(destination), "--force"]) == EXIT_OK
    assert json.loads(destination.read_text(encoding="utf-8"))["schema"] == (
        "org.topology-lantern.circuit-graph"
    )

    errors = io.StringIO()
    assert main(["ingest-spice", str(netlist), "--force"], stderr=errors) == EXIT_INPUT
    assert "--force requires --output" in errors.getvalue()


def test_all_ingest_sources_are_permanently_protected_outputs(tmp_path: Path) -> None:
    child = tmp_path / "child.sp"
    child_original = ".subckt leaf p n\nr1 p n 1k\n.ends\n"
    child.write_text(child_original, encoding="utf-8")
    entry = tmp_path / "entry.sp"
    entry.write_text(
        ".include child.sp\n.subckt top p n\nx1 p n leaf\n.ends\n",
        encoding="utf-8",
    )
    errors = io.StringIO()
    assert (
        main(
            ["ingest-spice", str(entry), "--output", str(child), "--force"],
            stderr=errors,
        )
        == EXIT_INPUT
    )
    assert "aliases protected input" in errors.getvalue()
    assert child.read_text(encoding="utf-8") == child_original


@pytest.mark.skipif(os.path.normcase("A") != os.path.normcase("a"), reason="case-folding FS")
def test_case_alias_of_input_is_protected(tmp_path: Path) -> None:
    netlist = tmp_path / "Circuit.SP"
    original = "r1 a b 1k\n"
    netlist.write_text(original, encoding="utf-8")
    case_alias = Path(str(netlist).swapcase())
    errors = io.StringIO()
    assert (
        main(
            ["ingest-spice", str(netlist), "--output", str(case_alias), "--force"],
            stderr=errors,
        )
        == EXIT_INPUT
    )
    assert netlist.read_text(encoding="utf-8") == original


def test_symlink_alias_of_input_is_protected(tmp_path: Path) -> None:
    netlist = tmp_path / "circuit.sp"
    original = "r1 a b 1k\n"
    netlist.write_text(original, encoding="utf-8")
    alias = tmp_path / "alias.sp"
    try:
        alias.symlink_to(netlist)
    except OSError:
        pytest.skip("symlinks are unavailable")
    errors = io.StringIO()
    assert (
        main(
            ["ingest-spice", str(netlist), "--output", str(alias), "--force"],
            stderr=errors,
        )
        == EXIT_INPUT
    )
    assert netlist.read_text(encoding="utf-8") == original
