"""Deterministic, evidence-preserving diagnosis of bounded provider logs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class LogObservation:
    """One bounded provider-log observation supplied by the project layer."""

    command: str
    source: str
    text: str


@dataclass(frozen=True, slots=True)
class _Rule:
    code: str
    category: str
    title: str
    expression: re.Pattern[str]
    repair: str
    next_step: str
    confidence: str = "high"


def _pattern(*expressions: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{item})" for item in expressions), re.IGNORECASE)


_RULES = (
    _Rule(
        "DISK_SPACE_EXHAUSTED",
        "resource",
        "The runtime could not write because storage is exhausted",
        _pattern(r"no space left on device", r"disk quota exceeded"),
        "Preview managed temporary data with `agentcfd clean .`, free enough storage, then retry.",
        "clean",
    ),
    _Rule(
        "MEMORY_EXHAUSTED",
        "resource",
        "The runtime exhausted available memory",
        _pattern(
            r"out of memory",
            r"cannot allocate memory",
            r"std::bad_alloc",
            r"bad allocation",
        ),
        "Reduce mesh/output demand or use a runtime with more memory; inspect the plan before retrying.",
        "plan",
    ),
    _Rule(
        "REQUIRED_FILE_MISSING",
        "configuration",
        "A required case or runtime file is missing",
        _pattern(r"cannot find file", r"no such file or directory", r"cannot open file"),
        "Check the generated-case evidence and runtime/container mount, then regenerate from the project model.",
        "check",
    ),
    _Rule(
        "BOUNDARY_CONFIGURATION_INVALID",
        "boundary-condition",
        "A mesh patch and field boundary condition do not agree",
        _pattern(
            r"cannot find patchfield entry",
            r"cannot find patch\b",
            r"patch .* not found",
            r"inconsistent .* patch",
        ),
        "Review named regions and boundary roles in `case.py`; do not edit generated OpenFOAM files as the fix.",
        "check",
    ),
    _Rule(
        "RUNTIME_SELECTION_UNKNOWN",
        "configuration",
        "The selected model, scheme, or runtime type is unavailable",
        _pattern(r"unknown .* type", r"unknown .* scheme", r"valid .* types\s*[:=]"),
        "Check provider/runtime compatibility and select only a capability advertised by the active OpenFOAM dialect.",
        "check",
    ),
    _Rule(
        "OPENFOAM_DICTIONARY_INVALID",
        "configuration",
        "OpenFOAM rejected a generated dictionary entry",
        _pattern(
            r"foam fatal io error",
            r"entry .* invalid input",
            r"excess tokens in stream",
            r"keyword .* is undefined",
        ),
        "Run the project check and inspect the cited generated entry; report reproducible provider-generation defects.",
        "check",
    ),
    _Rule(
        "MESH_QUALITY_FAILED",
        "mesh",
        "The generated mesh failed a quality or validity gate",
        _pattern(
            r"failed [1-9][0-9]* mesh checks",
            r"mesh(?: check)? failed",
            r"negative (?:cell )?volume",
            r"zero or negative face area",
            r"severely non[- ]orthogonal",
        ),
        "Revise declared mesh controls or geometry cleanup, then inspect the new plan before solving.",
        "plan",
    ),
    _Rule(
        "NUMERICAL_DIVERGENCE",
        "numerics",
        "The numerical solution became non-finite or unstable",
        _pattern(
            r"floating point exception",
            r"\bsigfpe\b",
            r"(?:residual|solution|field|value)[^\n]{0,80}\bnan\b",
            r"diverg(?:ed|ence)",
        ),
        "Inspect the final residual/Courant evidence, initial and boundary conditions, mesh quality, and time-step controls before retrying.",
        "logs",
    ),
    _Rule(
        "PARALLEL_RUNTIME_FAILED",
        "runtime",
        "The MPI or parallel runtime terminated the solve",
        _pattern(r"mpi_abort", r"mpi_err", r"pmix error", r"orte_error_log"),
        "Verify the MPI/container pairing and decomposition, then retry without changing the scientific model.",
        "check",
    ),
    _Rule(
        "PROCESS_TIMED_OUT",
        "runtime",
        "The provider command exceeded its execution limit",
        _pattern(
            r"timed out",
            r"timeout (?:expired|after)",
            r"time limit exceeded",
        ),
        "Increase the declared execution limit only after checking that progress was advancing and resource demand is acceptable.",
        "logs",
        "medium",
    ),
    _Rule(
        "PROCESS_KILLED",
        "runtime",
        "The operating environment killed the provider process",
        _pattern(r"(?:^|\s)killed(?:\s|$)", r"terminated by signal"),
        "Check host/container memory and scheduler limits before retrying; the log alone cannot prove which limit acted.",
        "plan",
        "medium",
    ),
    _Rule(
        "OPENFOAM_FATAL_ERROR",
        "provider",
        "OpenFOAM reported a fatal error",
        _pattern(r"foam fatal error", r"segmentation fault", r"core dumped"),
        "Read the cited provider evidence and repair the declared model or runtime; do not patch the generated case in place.",
        "logs",
        "medium",
    ),
)


def _evidence_line(text: str, expression: re.Pattern[str]) -> tuple[int, str] | None:
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if expression.search(lines[index]) and not (
            "trapfpe" in lines[index].lower()
            and "trapping enabled" in lines[index].lower()
        ):
            normalized = " ".join(lines[index].strip().split())
            return index + 1, normalized[:500]
    return None


def diagnose(observations: Iterable[LogObservation]) -> tuple[dict[str, object], ...]:
    """Classify known failure signatures without inventing an unobserved cause."""

    findings: list[dict[str, object]] = []
    seen: set[str] = set()
    materialized = tuple(observations)
    for rule in _RULES:
        for observation in reversed(materialized):
            evidence = _evidence_line(observation.text, rule.expression)
            if evidence is None or rule.code in seen:
                continue
            line, excerpt = evidence
            findings.append(
                {
                    "code": rule.code,
                    "category": rule.category,
                    "severity": "error",
                    "confidence": rule.confidence,
                    "title": rule.title,
                    "evidence": {
                        "command": observation.command,
                        "source": observation.source,
                        "line": line,
                        "excerpt": excerpt,
                    },
                    "repair": rule.repair,
                    "automatic_repair": False,
                    "next_step": rule.next_step,
                }
            )
            seen.add(rule.code)
            break
    return tuple(findings)
