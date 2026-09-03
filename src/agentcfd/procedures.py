"""Solution procedures express numerical intent without naming a backend."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class SteadyProcedure:
    relative_tolerance: float = 1.0e-8
    maximum_iterations: int = 500

    def __post_init__(self) -> None:
        if self.relative_tolerance <= 0.0:
            raise ValueError("Relative tolerance must be positive.")
        if self.maximum_iterations < 1:
            raise ValueError("Maximum iterations must be at least one.")

    def to_dict(self) -> dict[str, object]:
        return {"type": "steady", **asdict(self)}


def steady(*, relative_tolerance: float = 1.0e-8, maximum_iterations: int = 500) -> SteadyProcedure:
    return SteadyProcedure(relative_tolerance=relative_tolerance, maximum_iterations=maximum_iterations)
