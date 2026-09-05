"""Backend-neutral initial field declarations."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ._validation import finite_float, positive_float


def _vector3(value: object, *, name: str) -> tuple[float, float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError(f"{name} must contain exactly three components.")
    return tuple(finite_float(item, name=name) for item in value)


@dataclass(frozen=True, slots=True)
class UniformInitialization:
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gauge_pressure: float = 0.0
    temperature: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "velocity",
            _vector3(self.velocity, name="Initial velocity"),
        )
        object.__setattr__(
            self,
            "gauge_pressure",
            finite_float(self.gauge_pressure, name="Initial gauge pressure"),
        )
        if self.temperature is not None:
            object.__setattr__(
                self,
                "temperature",
                positive_float(self.temperature, name="Initial temperature"),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "uniform",
            "velocity": list(self.velocity),
            "gauge_pressure": self.gauge_pressure,
            "temperature": self.temperature,
        }


@dataclass(frozen=True, slots=True)
class PotentialFlowInitialization:
    """Geometry-aware potential-flow guess, comparable to hybrid initialization."""

    maximum_iterations: int = 10

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_iterations, bool)
            or not isinstance(self.maximum_iterations, int)
            or self.maximum_iterations < 1
        ):
            raise ValueError(
                "Initialization iterations must be an integer of at least one."
            )

    def to_dict(self) -> dict[str, object]:
        return {"type": "potential-flow", **asdict(self)}


@dataclass(frozen=True, slots=True)
class PreviousResultInitialization:
    """Initialize from a content-addressed AgentCFD result artifact."""

    result: str
    minimum_trust: str = "converged"

    def __post_init__(self) -> None:
        if not isinstance(self.result, str) or not self.result.strip():
            raise ValueError(
                "Previous-result initialization requires a result path or ID."
            )
        object.__setattr__(self, "result", self.result.strip())
        if self.minimum_trust not in {"computed", "converged", "verified", "validated"}:
            raise ValueError("Previous-result minimum trust level is invalid.")

    def to_dict(self) -> dict[str, object]:
        return {"type": "previous-result", **asdict(self)}


Initialization = (
    UniformInitialization | PotentialFlowInitialization | PreviousResultInitialization
)


def uniform(
    *,
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0),
    gauge_pressure: float = 0.0,
    temperature: float | None = None,
) -> UniformInitialization:
    return UniformInitialization(
        velocity=velocity,
        gauge_pressure=gauge_pressure,
        temperature=temperature,
    )


def potential_flow(*, maximum_iterations: int = 10) -> PotentialFlowInitialization:
    return PotentialFlowInitialization(maximum_iterations=maximum_iterations)


def previous_result(
    result: str,
    *,
    minimum_trust: str = "converged",
) -> PreviousResultInitialization:
    return PreviousResultInitialization(result=result, minimum_trust=minimum_trust)


__all__ = [
    "Initialization",
    "PotentialFlowInitialization",
    "PreviousResultInitialization",
    "UniformInitialization",
    "potential_flow",
    "previous_result",
    "uniform",
]
