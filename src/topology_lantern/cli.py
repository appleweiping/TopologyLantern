"""Command-line interface for generation, circuit evidence, and trace replay."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from topology_lantern._version import __version__
from topology_lantern.benchmark import benchmark_json
from topology_lantern.canonical import candidate_id, topology_signature
from topology_lantern.circuit import CircuitGraph, circuit_graph_json
from topology_lantern.compare import diff_results, render_diff
from topology_lantern.emit import candidate_spice, diff_json, result_json, result_text
from topology_lantern.explain import replay_rule_ids
from topology_lantern.graph_codec import (
    CompactCircuitGraph,
    ConnectivityView,
    PinCircuitGraph,
    compact_graph,
    compact_to_pin,
    connectivity_graph_json,
    load_connectivity_graph,
    pin_graph,
    pin_to_compact,
)
from topology_lantern.graph_sequence import (
    decode_graph_sequence,
    encode_graph_sequence,
    graph_sequence_json,
    load_graph_sequence,
)
from topology_lantern.layout import (
    layout_inference_report,
    layout_report_json,
    load_layout_constraints,
)
from topology_lantern.search import candidate_from_state, generate_candidates
from topology_lantern.sequence_baseline import (
    build_sequence_examples,
    evaluate_sequence_baseline,
    evaluation_report_json,
    load_sequence_checkpoint,
    sample_report_json,
    sample_sequence_baseline,
    sequence_checkpoint_json,
    train_sequence_baseline,
)
from topology_lantern.spec import DesignSpec, _load_json_object
from topology_lantern.spice import load_spice
from topology_lantern.types import LanternError, ReplayError, SpecError

EXIT_OK = 0
EXIT_EMPTY = 2
EXIT_INPUT = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topology-lantern",
        description=(
            "Generate bounded analog topology candidates and inspect typed circuit evidence."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="search from a JSON design specification")
    generate.add_argument("spec")
    generate.add_argument("--limit", type=int)
    generate.add_argument("--format", choices=("text", "json", "spice"), default="text")
    generate.add_argument("--candidate", type=int, default=1, help="1-based candidate for SPICE")
    generate.add_argument("--pretty", action="store_true")
    generate.add_argument("--output")
    generate.add_argument("--force", action="store_true", help="atomically replace output")
    generate.add_argument(
        "--ledger",
        action="store_true",
        help="add the per-rule search ledger; omitted by default so the report "
        "stays exactly what existing readers validate",
    )

    diff = commands.add_parser("diff", help="compare the topologies two specifications admit")
    diff.add_argument("left")
    diff.add_argument("right")
    diff.add_argument("--limit", type=int)
    diff.add_argument("--format", choices=("text", "json"), default="text")
    diff.add_argument("--pretty", action="store_true")
    diff.add_argument("--output")
    diff.add_argument("--force", action="store_true", help="atomically replace output")

    benchmark = commands.add_parser(
        "benchmark", help="emit a machine-readable topology-and-sizing benchmark"
    )
    benchmark.add_argument("spec")
    benchmark.add_argument("--limit", type=int)
    benchmark.add_argument("--pretty", action="store_true")
    benchmark.add_argument("--output")
    benchmark.add_argument("--force", action="store_true", help="atomically replace output")

    validate = commands.add_parser("validate-spec", help="validate and fingerprint a spec")
    validate.add_argument("spec")
    validate.add_argument("--output")
    validate.add_argument("--force", action="store_true", help="atomically replace output")

    explain = commands.add_parser("explain", help="explain one candidate from a JSON report")
    explain.add_argument("report")
    explain.add_argument("candidate_id")
    explain.add_argument("--output")
    explain.add_argument("--force", action="store_true", help="atomically replace output")

    replay = commands.add_parser("replay", help="replay one report trace against its spec")
    replay.add_argument("spec")
    replay.add_argument("report")
    replay.add_argument("candidate_id")
    replay.add_argument("--output")
    replay.add_argument("--force", action="store_true", help="atomically replace output")

    ingest = commands.add_parser(
        "ingest-spice", help="convert a bounded hierarchical SPICE subset to a typed graph"
    )
    ingest.add_argument("netlist")
    ingest.add_argument("--top", help="explicit top subcircuit; inferred when unambiguous")
    ingest.add_argument("--pretty", action="store_true")
    ingest.add_argument("--output")
    ingest.add_argument("--force", action="store_true", help="atomically replace output")

    connectivity = commands.add_parser(
        "connectivity-graph",
        help="emit a lossless compact owner/net or explicit pin/net circuit view",
    )
    connectivity.add_argument("netlist")
    connectivity.add_argument("--top", help="explicit top subcircuit; inferred when unambiguous")
    connectivity.add_argument(
        "--view",
        choices=tuple(item.value for item in ConnectivityView),
        default=ConnectivityView.COMPACT.value,
    )
    connectivity.add_argument("--pretty", action="store_true")
    connectivity.add_argument("--output")
    connectivity.add_argument("--force", action="store_true", help="atomically replace output")

    transcode = commands.add_parser(
        "transcode-connectivity",
        help="strictly validate and convert a version-1 connectivity JSON view",
    )
    transcode.add_argument("graph")
    transcode.add_argument(
        "--view",
        choices=tuple(item.value for item in ConnectivityView),
        required=True,
    )
    transcode.add_argument("--pretty", action="store_true")
    transcode.add_argument("--output")
    transcode.add_argument("--force", action="store_true", help="atomically replace output")

    sequence = commands.add_parser(
        "encode-graph-sequence", help="encode connectivity as Euler trails"
    )
    sequence.add_argument("graph")
    sequence.add_argument("--seed", type=int, default=0)
    sequence.add_argument("--pretty", action="store_true")
    sequence.add_argument("--output")
    sequence.add_argument("--force", action="store_true", help="atomically replace output")
    decode_sequence = commands.add_parser(
        "decode-graph-sequence", help="reconstruct a compact graph from checked Euler trails"
    )
    decode_sequence.add_argument("sequence")
    decode_sequence.add_argument("--pretty", action="store_true")
    decode_sequence.add_argument("--output")
    decode_sequence.add_argument("--force", action="store_true", help="atomically replace output")

    layout = commands.add_parser(
        "layout-evidence",
        help="validate layout intent and emit separate evidence-backed candidates",
    )
    layout.add_argument("netlist")
    layout.add_argument("--top", help="explicit top subcircuit; inferred when unambiguous")
    layout.add_argument("--constraints", help="optional version-1 user constraint JSON")
    layout.add_argument("--pretty", action="store_true")
    layout.add_argument("--output")
    layout.add_argument("--force", action="store_true", help="atomically replace output")

    train = commands.add_parser(
        "baseline-train",
        help="fit an auditable rule-bigram baseline (not a trained ML model)",
    )
    train.add_argument("specs", nargs="+", help="training design specifications")
    train.add_argument(
        "--heldout-spec",
        action="append",
        default=[],
        help="held-out spec used only for lineage-leakage validation; repeatable",
    )
    train.add_argument("--candidate-limit", type=int)
    train.add_argument("--augment-polarity", action="store_true")
    train.add_argument("--pretty", action="store_true")
    train.add_argument("--output")
    train.add_argument("--force", action="store_true", help="atomically replace output")

    sample = commands.add_parser(
        "baseline-sample",
        help="sample replayable sequences through structural constraints",
    )
    sample.add_argument("checkpoint")
    sample.add_argument("spec")
    sample.add_argument("--draws", type=int, default=16)
    sample.add_argument("--seed", type=int, default=0)
    sample.add_argument("--pretty", action="store_true")
    sample.add_argument("--output")
    sample.add_argument("--force", action="store_true", help="atomically replace output")

    evaluate = commands.add_parser(
        "baseline-evaluate",
        help="measure held-out structural sequence metrics with leakage checks",
    )
    evaluate.add_argument("checkpoint")
    evaluate.add_argument("specs", nargs="+", help="held-out design specifications")
    evaluate.add_argument("--candidate-limit", type=int)
    evaluate.add_argument("--draws-per-spec", type=int, default=16)
    evaluate.add_argument("--seed", type=int, default=0)
    evaluate.add_argument("--augment-polarity", action="store_true")
    evaluate.add_argument("--pretty", action="store_true")
    evaluate.add_argument("--output")
    evaluate.add_argument("--force", action="store_true", help="atomically replace output")
    return parser


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _paths_alias(left: Path, right: Path) -> bool:
    if _path_key(left) == _path_key(right):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _write(
    text: str,
    destination: str | None,
    stdout: TextIO,
    *,
    protected_paths: Sequence[str | Path] = (),
    force: bool = False,
) -> None:
    if destination is None:
        if force:
            raise SpecError("--force requires --output")
        stdout.write(text)
        return
    output = Path(destination)
    for protected in protected_paths:
        if _paths_alias(output, Path(protected)):
            raise SpecError(f"output aliases protected input: {protected}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".topology-lantern-", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, output)
        else:
            try:
                os.link(temporary, output)
            except FileExistsError as error:
                raise SpecError(
                    f"output already exists; use --force to replace: {output}"
                ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _graph_source_paths(netlist: str, graph: CircuitGraph) -> tuple[Path, ...]:
    root = Path(netlist).resolve().parent
    return tuple(root / source for source in graph.sources)


def _load_report(path: str) -> Mapping[str, object]:
    value = _load_json_object(path, "result report")
    schema_version = value.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise SpecError("result report schema_version must be 1")
    tool = value.get("tool")
    if (
        not isinstance(tool, Mapping)
        or set(tool) != {"name", "version"}
        or tool.get("name") != "TopologyLantern"
        or not isinstance(tool.get("version"), str)
    ):
        raise SpecError("result report was not produced by TopologyLantern")
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        raise SpecError("result report candidates must be an array")
    allowed = {
        "schema_version",
        "tool",
        "spec_fingerprint",
        "search",
        "rule_catalog",
        "candidates",
    }
    if set(value) != allowed:
        raise SpecError("result report has missing or unknown top-level fields")
    if not isinstance(value.get("spec_fingerprint"), str):
        raise SpecError("result report spec_fingerprint must be a string")
    search = value.get("search")
    if not isinstance(search, Mapping):
        raise SpecError("result report search must be an object")
    expected_search = {
        "requested_limit",
        "explored_states",
        "pruned_states",
        "duplicate_states",
        "exhausted",
    }
    if set(search) != expected_search:
        raise SpecError("result report search has missing or unknown fields")
    for field in expected_search - {"exhausted"}:
        item = search.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise SpecError(f"result report search {field} must be a non-negative integer")
    if search.get("requested_limit", 0) <= 0:
        raise SpecError("result report search requested_limit must be positive")
    if not isinstance(search.get("exhausted"), bool):
        raise SpecError("result report search exhausted must be a boolean")
    catalog = value.get("rule_catalog")
    if not isinstance(catalog, list) or not all(isinstance(item, str) for item in catalog):
        raise SpecError("result report rule_catalog must be an array of strings")
    return value


def _candidate_mapping(report: Mapping[str, object], candidate_id: str) -> Mapping[str, object]:
    candidates = report.get("candidates", [])
    if not isinstance(candidates, list):
        raise SpecError("result report candidates must be an array")
    for candidate in candidates:
        if isinstance(candidate, Mapping) and candidate.get("candidate_id") == candidate_id:
            expected = {
                "candidate_id",
                "signature",
                "pareto_rank",
                "score",
                "topology",
                "facts",
                "metrics",
                "violations",
                "trace",
            }
            if set(candidate) != expected:
                raise SpecError("candidate has missing or unknown fields")
            if not isinstance(candidate.get("signature"), str):
                raise SpecError("candidate signature must be a string")
            for field in ("pareto_rank", "score"):
                value = candidate.get(field)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise SpecError(f"candidate {field} must be an integer")
            if not isinstance(candidate.get("topology"), Mapping):
                raise SpecError("candidate topology must be an object")
            for field in ("facts", "violations", "trace"):
                if not isinstance(candidate.get(field), list):
                    raise SpecError(f"candidate {field} must be an array")
            if not isinstance(candidate.get("metrics"), Mapping):
                raise SpecError("candidate metrics must be an object")
            return candidate
    raise SpecError(f"candidate ID not found in report: {candidate_id}")


def _report_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise SpecError(f"{field} must be a string")
    rendered: list[str] = []
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for character in value:
        codepoint = ord(character)
        if character in escapes:
            rendered.append(escapes[character])
        elif codepoint < 32 or 127 <= codepoint <= 159:
            rendered.append(f"\\x{codepoint:02x}")
        elif (
            not character.isprintable()
            or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            or unicodedata.bidirectional(character)
            in {"BN", "LRE", "LRI", "LRO", "PDF", "PDI", "RLE", "RLI", "RLO", "FSI"}
        ):
            width = 4 if codepoint <= 0xFFFF else 8
            prefix = "u" if width == 4 else "U"
            rendered.append(f"\\{prefix}{codepoint:0{width}x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def _explain_mapping(candidate: Mapping[str, object]) -> str:
    rendered_id = _report_text(candidate.get("candidate_id"), "candidate candidate_id")
    lines = [
        "UNVERIFIED STORED REPORT EVIDENCE (use replay to verify core fields)",
        f"{rendered_id}: Pareto front {candidate['pareto_rank']}, "
        f"score {candidate.get('score', '?')}",
        f"signature: {_report_text(candidate.get('signature'), 'candidate signature')}",
        "derivation:",
    ]
    trace = candidate.get("trace", [])
    if not isinstance(trace, list):
        raise SpecError("candidate trace must be an array")
    for step in trace:
        if not isinstance(step, Mapping):
            raise SpecError("candidate trace steps must be objects")
        index = step.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise SpecError("candidate trace index must be an integer")
        rule_id = _report_text(step.get("rule_id"), "candidate trace rule_id")
        summary = _report_text(step.get("summary"), "candidate trace summary")
        rationale = _report_text(step.get("rationale"), "candidate trace rationale")
        lines.append(f"  {index}. {rule_id} — {summary}")
        lines.append(f"     {rationale}")
    violations = candidate.get("violations", [])
    if not isinstance(violations, list):
        raise SpecError("candidate violations must be an array")
    if violations:
        lines.append("review notes:")
        for item in violations:
            if not isinstance(item, Mapping):
                raise SpecError("candidate violations must contain objects")
            severity = _report_text(item.get("severity"), "candidate violation severity")
            code = _report_text(item.get("code"), "candidate violation code")
            message = _report_text(item.get("message"), "candidate violation message")
            lines.append(f"  - [{severity}] {code}: {message}")
    else:
        lines.append("review notes: none")
    return "\n".join(lines) + "\n"


def _trace_ids(candidate: Mapping[str, object]) -> list[str]:
    trace = candidate.get("trace")
    if not isinstance(trace, list):
        raise ReplayError("candidate trace must be an array")
    result: list[str] = []
    for step in trace:
        if not isinstance(step, Mapping) or not isinstance(step.get("rule_id"), str):
            raise ReplayError("candidate trace contains an invalid rule ID")
        result.append(step["rule_id"])
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    try:
        args = _parser().parse_args(argv)
        if args.command == "ingest-spice":
            graph = load_spice(args.netlist, top=args.top)
            _write(
                circuit_graph_json(graph, pretty=args.pretty),
                args.output,
                output,
                protected_paths=_graph_source_paths(args.netlist, graph),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "connectivity-graph":
            graph = load_spice(args.netlist, top=args.top)
            view = (
                pin_graph(graph)
                if args.view == ConnectivityView.PIN_LEVEL
                else compact_graph(graph)
            )
            _write(
                connectivity_graph_json(view, pretty=args.pretty),
                args.output,
                output,
                protected_paths=_graph_source_paths(args.netlist, graph),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "transcode-connectivity":
            source_view = load_connectivity_graph(args.graph)
            target_view: CompactCircuitGraph | PinCircuitGraph
            if args.view == ConnectivityView.COMPACT:
                target_view = (
                    source_view
                    if isinstance(source_view, CompactCircuitGraph)
                    else pin_to_compact(source_view)
                )
            else:
                target_view = (
                    compact_to_pin(source_view)
                    if isinstance(source_view, CompactCircuitGraph)
                    else source_view
                )
            _write(
                connectivity_graph_json(target_view, pretty=args.pretty),
                args.output,
                output,
                protected_paths=(args.graph,),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "encode-graph-sequence":
            sequence_source = load_connectivity_graph(args.graph)
            compact_source = (
                sequence_source
                if isinstance(sequence_source, CompactCircuitGraph)
                else pin_to_compact(sequence_source)
            )
            sequence = encode_graph_sequence(compact_source, seed=args.seed)
            _write(
                graph_sequence_json(sequence, pretty=args.pretty),
                args.output,
                output,
                protected_paths=(args.graph,),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "decode-graph-sequence":
            reconstructed = decode_graph_sequence(load_graph_sequence(args.sequence))
            _write(
                connectivity_graph_json(reconstructed, pretty=args.pretty),
                args.output,
                output,
                protected_paths=(args.sequence,),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "layout-evidence":
            graph = load_spice(args.netlist, top=args.top)
            constraints = (
                load_layout_constraints(args.constraints, graph) if args.constraints else None
            )
            evidence_report = layout_inference_report(graph, constraints)
            _write(
                layout_report_json(evidence_report, pretty=args.pretty),
                args.output,
                output,
                protected_paths=(
                    *_graph_source_paths(args.netlist, graph),
                    *((args.constraints,) if args.constraints else ()),
                ),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "baseline-train":
            training = build_sequence_examples(
                args.specs,
                augment_polarity=args.augment_polarity,
                limit=args.candidate_limit,
            )
            heldout = (
                build_sequence_examples(
                    args.heldout_spec,
                    augment_polarity=args.augment_polarity,
                    limit=args.candidate_limit,
                )
                if args.heldout_spec
                else ()
            )
            checkpoint = train_sequence_baseline(training, heldout=heldout)
            _write(
                sequence_checkpoint_json(checkpoint, pretty=args.pretty),
                args.output,
                output,
                protected_paths=(*args.specs, *args.heldout_spec),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "baseline-sample":
            checkpoint = load_sequence_checkpoint(args.checkpoint)
            sample_report = sample_sequence_baseline(
                checkpoint,
                args.spec,
                draws=args.draws,
                seed=args.seed,
            )
            _write(
                sample_report_json(sample_report, pretty=args.pretty),
                args.output,
                output,
                protected_paths=(args.checkpoint, args.spec),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "baseline-evaluate":
            checkpoint = load_sequence_checkpoint(args.checkpoint)
            heldout = build_sequence_examples(
                args.specs,
                augment_polarity=args.augment_polarity,
                limit=args.candidate_limit,
            )
            evaluation_report = evaluate_sequence_baseline(
                checkpoint,
                heldout,
                draws_per_spec=args.draws_per_spec,
                seed=args.seed,
            )
            _write(
                evaluation_report_json(evaluation_report, pretty=args.pretty),
                args.output,
                output,
                protected_paths=(args.checkpoint, *args.specs),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "generate":
            result = generate_candidates(args.spec, limit=args.limit)
            if args.format == "json":
                rendered = result_json(result, pretty=args.pretty, ledger=args.ledger)
            elif args.format == "spice":
                if not 1 <= args.candidate <= len(result.candidates):
                    raise SpecError("--candidate is outside the generated candidate range")
                rendered = candidate_spice(result.candidates[args.candidate - 1])
            else:
                rendered = result_text(result)
            _write(
                rendered,
                args.output,
                output,
                protected_paths=(args.spec,),
                force=args.force,
            )
            return EXIT_OK if result.candidates else EXIT_EMPTY
        if args.command == "diff":
            left = generate_candidates(args.left, limit=args.limit)
            right = generate_candidates(args.right, limit=args.limit)
            comparison = diff_results(left, right)
            rendered = (
                diff_json(comparison, pretty=args.pretty)
                if args.format == "json"
                else render_diff(comparison) + "\n"
            )
            _write(
                rendered,
                args.output,
                output,
                protected_paths=(args.left, args.right),
                force=args.force,
            )
            # A comparison that found nothing to report is not an error, so the
            # empty status is reserved for a run that produced no topologies at
            # all on either side.
            return EXIT_OK if (left.candidates or right.candidates) else EXIT_EMPTY
        if args.command == "validate-spec":
            spec = DesignSpec.from_json(args.spec)
            _write(
                f"valid specification: sha256:{spec.fingerprint()}\n",
                args.output,
                output,
                protected_paths=(args.spec,),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "benchmark":
            spec = DesignSpec.from_json(args.spec)
            result = generate_candidates(spec, limit=args.limit)
            _write(
                benchmark_json(spec, result, pretty=args.pretty),
                args.output,
                output,
                protected_paths=(args.spec,),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "explain":
            report = _load_report(args.report)
            _write(
                _explain_mapping(_candidate_mapping(report, args.candidate_id)),
                args.output,
                output,
                protected_paths=(args.report,),
                force=args.force,
            )
            return EXIT_OK
        if args.command == "replay":
            spec = DesignSpec.from_json(args.spec)
            report = _load_report(args.report)
            expected_fingerprint = report.get("spec_fingerprint")
            if expected_fingerprint != spec.fingerprint():
                raise ReplayError(
                    "report specification fingerprint does not match the supplied spec"
                )
            candidate = _candidate_mapping(report, args.candidate_id)
            search = report["search"]
            if not isinstance(search, Mapping):
                raise ReplayError("report search context is invalid")
            requested_limit = search.get("requested_limit")
            if isinstance(requested_limit, bool) or not isinstance(requested_limit, int):
                raise ReplayError("report requested limit is invalid")
            state = replay_rule_ids(spec, _trace_ids(candidate))
            if state.obligations:
                raise ReplayError("trace replay ended before all obligations were resolved")
            actual = topology_signature(
                state.topology,
                max_permutations=spec.limits.max_canonical_permutations,
            )
            expected = candidate.get("signature")
            if actual != expected:
                raise ReplayError(f"signature mismatch: replayed {actual}, report has {expected}")
            if candidate.get("candidate_id") != candidate_id(actual):
                raise ReplayError("candidate ID does not match the verified topology signature")
            trusted = candidate_from_state(spec, state)
            if trusted is None:
                raise ReplayError("replayed topology violates the final candidate contract")
            trusted_mapping = trusted.as_dict()
            for field in ("topology", "facts", "trace", "metrics", "violations"):
                if candidate.get(field) != trusted_mapping[field]:
                    raise ReplayError(f"report {field} does not match replayed evidence")
            regenerated = generate_candidates(spec, limit=requested_limit)
            if regenerated.as_dict() != dict(report):
                raise ReplayError("report does not match the regenerated search result")
            _write(
                f"replay core evidence verified: {args.candidate_id} sha256:{actual}\n",
                args.output,
                output,
                protected_paths=(args.spec, args.report),
                force=args.force,
            )
            return EXIT_OK
    except (LanternError, OSError, UnicodeError, ValueError) as exc:
        errors.write(f"topology-lantern: {exc}\n")
        return EXIT_INPUT
    return EXIT_INPUT


def entrypoint() -> None:
    """Convert the library-friendly status code into a process status."""

    raise SystemExit(main())
