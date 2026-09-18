"""Deterministic, versioned legal rule engine.

``checks``     the test implementations, pure functions over reviewed values
``selection``  which rule versions apply to an inspection, and why
``engine``     runs the applicable rules and writes findings
``simulator``  scenario runner that gates approval
"""

from .checks import ENGINE_VERSION, CheckInput, CheckResult, available_checks, run_check
from .engine import EngineResult, evaluate_inspection, summarise_outcomes
from .selection import Applicability, SelectionContext, evaluate, select
from .simulator import (
    MANDATORY_KINDS,
    SCOPE_EXCLUDED,
    Scenario,
    ScenarioKind,
    SimulationReport,
    boundary_scenarios,
    default_scenarios,
    required_kinds_for,
    simulate,
)

__all__ = [
    "ENGINE_VERSION",
    "MANDATORY_KINDS",
    "SCOPE_EXCLUDED",
    "Applicability",
    "CheckInput",
    "CheckResult",
    "EngineResult",
    "Scenario",
    "ScenarioKind",
    "SelectionContext",
    "SimulationReport",
    "available_checks",
    "boundary_scenarios",
    "default_scenarios",
    "evaluate",
    "evaluate_inspection",
    "required_kinds_for",
    "run_check",
    "select",
    "simulate",
    "summarise_outcomes",
]
