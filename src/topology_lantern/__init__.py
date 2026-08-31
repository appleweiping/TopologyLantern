"""Explainable, deterministic analog topology candidate generation."""

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
    "candidate_spice",
    "explain_candidate",
    "generate_candidates",
    "result_json",
    "result_text",
    "verify_replay",
]

__version__ = "0.1.0"
