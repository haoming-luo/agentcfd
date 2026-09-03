"""Structured simulation results and learning-ready samples."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class Quantity:
    value: float
    unit: str


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    value: float | str | None = None
    limit: float | str | None = None
    message: str = ""


@dataclass(slots=True)
class SimulationResult:
    status: str
    converged: bool
    provider: str
    quantities: dict[str, Quantity]
    checks: tuple[Check, ...]
    arrays: dict[str, list[float]] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    messages: tuple[str, ...] = ()
    schema: str = "agentcfd.simulation-result/0.1"

    @property
    def accepted(self) -> bool:
        return self.status == "completed" and self.converged and all(check.passed for check in self.checks)

    def require_accepted(self) -> "SimulationResult":
        if not self.accepted:
            failed = ", ".join(check.name for check in self.checks if not check.passed)
            raise RuntimeError(f"Simulation result is not accepted. Failed checks: {failed or 'execution'}")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "converged": self.converged,
            "accepted": self.accepted,
            "provider": self.provider,
            "quantities": {name: asdict(value) for name, value in self.quantities.items()},
            "checks": [asdict(check) for check in self.checks],
            "arrays": self.arrays,
            "provenance": self.provenance,
            "messages": list(self.messages),
        }

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return target

    def to_sample(
        self,
        *,
        parameters: Mapping[str, float],
        responses: Sequence[str],
    ) -> dict[str, Any]:
        """Create a framework-neutral sample for surrogates or neural operators."""

        self.require_accepted()
        missing = [name for name in responses if name not in self.quantities]
        if missing:
            raise KeyError(f"Unknown response quantities: {', '.join(missing)}")
        return {
            "schema": "agentcae.scientific-sample/0.1",
            "source": "agentcfd",
            "parameters": {name: float(value) for name, value in parameters.items()},
            "responses": {
                name: asdict(self.quantities[name])
                for name in responses
            },
            "provenance": dict(self.provenance),
        }
