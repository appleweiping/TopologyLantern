"""Explainable, deterministic analog topology candidate generation."""

from topology_lantern._version import __version__
from topology_lantern.benchmark import benchmark_json, sizing_benchmark
from topology_lantern.emit import candidate_spice, result_json, result_text
from topology_lantern.explain import explain_candidate, verify_replay
from topology_lantern.search import generate_candidates
from topology_lantern.spec import DesignSpec, Objectives, SearchLimits
from topology_lantern.types import Candidate, GenerationResult, SpecError

__all__ = [
    "Candidate",
    "DesignSpec",
    "GenerationResult",
    "Objectives",
    "SearchLimits",
    "SpecError",
    "__version__",
    "benchmark_json",
    "candidate_spice",
    "explain_candidate",
    "generate_candidates",
    "result_json",
    "result_text",
    "sizing_benchmark",
    "verify_replay",
]
