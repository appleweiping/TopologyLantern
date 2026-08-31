"""Command-line interface for generation, inspection, and trace replay."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from topology_lantern.canonical import candidate_id, topology_signature
from topology_lantern.emit import candidate_spice, result_json, result_text
from topology_lantern.explain import replay_rule_ids
from topology_lantern.search import candidate_from_state, generate_candidates
from topology_lantern.spec import DesignSpec
from topology_lantern.types import LanternError, ReplayError, SpecError

EXIT_OK = 0
EXIT_EMPTY = 2
EXIT_INPUT = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topology-lantern",
        description="Generate bounded, explainable conceptual analog topology candidates.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="search from a JSON design specification")
    generate.add_argument("spec")
    generate.add_argument("--limit", type=int)
    generate.add_argument("--format", choices=("text", "json", "spice"), default="text")
    generate.add_argument("--candidate", type=int, default=1, help="1-based candidate for SPICE")
    generate.add_argument("--pretty", action="store_true")
    generate.add_argument("--output")

    validate = commands.add_parser("validate-spec", help="validate and fingerprint a spec")
    validate.add_argument("spec")

    explain = commands.add_parser("explain", help="explain one candidate from a JSON report")
    explain.add_argument("report")
    explain.add_argument("candidate_id")

    replay = commands.add_parser("replay", help="replay one report trace against its spec")
    replay.add_argument("spec")
    replay.add_argument("report")
    replay.add_argument("candidate_id")
    return parser


def _write(text: str, destination: str | None, stdout: TextIO) -> None:
    if destination:
        Path(destination).write_text(text, encoding="utf-8", newline="\n")
    else:
        stdout.write(text)


def _load_report(path: str) -> Mapping[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpecError(f"cannot load result report: {exc}") from exc
    if not isinstance(value, Mapping):
        raise SpecError("result report must be a JSON object")
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
        if args.command == "generate":
            result = generate_candidates(args.spec, limit=args.limit)
            if args.format == "json":
                rendered = result_json(result, pretty=args.pretty)
            elif args.format == "spice":
                if not 1 <= args.candidate <= len(result.candidates):
                    raise SpecError("--candidate is outside the generated candidate range")
                rendered = candidate_spice(result.candidates[args.candidate - 1])
            else:
                rendered = result_text(result)
            _write(rendered, args.output, output)
            return EXIT_OK if result.candidates else EXIT_EMPTY
        if args.command == "validate-spec":
            spec = DesignSpec.from_json(args.spec)
            output.write(f"valid specification: sha256:{spec.fingerprint()}\n")
            return EXIT_OK
        if args.command == "explain":
            report = _load_report(args.report)
            output.write(_explain_mapping(_candidate_mapping(report, args.candidate_id)))
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
            output.write(f"replay core evidence verified: {args.candidate_id} sha256:{actual}\n")
            return EXIT_OK
    except (LanternError, OSError, UnicodeError, ValueError) as exc:
        errors.write(f"topology-lantern: {exc}\n")
        return EXIT_INPUT
    return EXIT_INPUT
