from __future__ import annotations

import io
import json
from pathlib import Path

from topology_lantern.cli import EXIT_INPUT, EXIT_OK, main


def write_spec(path: Path, name: str, voltage: float) -> None:
    path.write_text(
        json.dumps({"name": name, "supply_voltage": voltage}),
        encoding="utf-8",
    )


def test_baseline_cli_train_sample_and_heldout_evaluate(tmp_path: Path) -> None:
    training = tmp_path / "training.json"
    heldout = tmp_path / "heldout.json"
    write_spec(training, "training", 1.2)
    write_spec(heldout, "heldout", 2.4)
    checkpoint = tmp_path / "checkpoint.json"
    assert (
        main(
            [
                "baseline-train",
                str(training),
                "--heldout-spec",
                str(heldout),
                "--candidate-limit",
                "3",
                "--augment-polarity",
                "--pretty",
                "--output",
                str(checkpoint),
            ]
        )
        == EXIT_OK
    )
    checkpoint_value = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_value["model_kind"] == "laplace-bigram-rule-baseline"
    assert len(checkpoint_value["training_lineages"]) == 1

    samples = tmp_path / "samples.json"
    assert (
        main(
            [
                "baseline-sample",
                str(checkpoint),
                str(heldout),
                "--draws",
                "4",
                "--seed",
                "17",
                "--output",
                str(samples),
            ]
        )
        == EXIT_OK
    )
    sample_value = json.loads(samples.read_text(encoding="utf-8"))
    assert sample_value["requested_draws"] == 4
    assert len(sample_value["draws"]) == 4

    evaluation = io.StringIO()
    assert (
        main(
            [
                "baseline-evaluate",
                str(checkpoint),
                str(heldout),
                "--candidate-limit",
                "3",
                "--augment-polarity",
                "--draws-per-spec",
                "4",
            ],
            stdout=evaluation,
        )
        == EXIT_OK
    )
    evaluation_value = json.loads(evaluation.getvalue())
    assert evaluation_value["leakage_check"] == "passed_lineage_disjoint"
    assert evaluation_value["requested_draws"] == 8


def test_baseline_cli_permanently_protects_checkpoint_and_spec_inputs(tmp_path: Path) -> None:
    training = tmp_path / "training.json"
    heldout = tmp_path / "heldout.json"
    write_spec(training, "training", 1.2)
    write_spec(heldout, "heldout", 2.4)
    checkpoint = tmp_path / "checkpoint.json"
    assert (
        main(
            [
                "baseline-train",
                str(training),
                "--output",
                str(checkpoint),
            ]
        )
        == EXIT_OK
    )
    originals = {
        training: training.read_bytes(),
        heldout: heldout.read_bytes(),
        checkpoint: checkpoint.read_bytes(),
    }
    cases = [
        (["baseline-train", str(training)], training),
        (["baseline-sample", str(checkpoint), str(heldout), "--draws", "1"], checkpoint),
        (
            [
                "baseline-evaluate",
                str(checkpoint),
                str(heldout),
                "--draws-per-spec",
                "1",
            ],
            heldout,
        ),
    ]
    for command, protected in cases:
        errors = io.StringIO()
        assert (
            main(
                [*command, "--output", str(protected), "--force"],
                stderr=errors,
            )
            == EXIT_INPUT
        )
        assert "aliases protected input" in errors.getvalue()
        assert protected.read_bytes() == originals[protected]


def test_baseline_cli_reports_bad_checkpoint_and_lineage_leakage(tmp_path: Path) -> None:
    selected = tmp_path / "same.json"
    write_spec(selected, "same", 1.8)
    errors = io.StringIO()
    assert (
        main(
            [
                "baseline-train",
                str(selected),
                "--heldout-spec",
                str(selected),
            ],
            stderr=errors,
        )
        == EXIT_INPUT
    )
    assert "lineage leakage" in errors.getvalue()

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    errors = io.StringIO()
    assert (
        main(
            ["baseline-sample", str(bad), str(selected), "--draws", "1"],
            stderr=errors,
        )
        == EXIT_INPUT
    )
    assert errors.getvalue().startswith("topology-lantern:")
