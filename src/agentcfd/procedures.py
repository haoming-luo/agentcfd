"""Solution procedures express numerical intent without naming a backend."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ._validation import integer_at_least, positive_float


@dataclass(frozen=True, slots=True)
class SteadyProcedure:
    relative_tolerance: float = 1.0e-8
    maximum_iterations: int = 500

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relative_tolerance",
            positive_float(self.relative_tolerance, name="Relative tolerance"),
        )
        object.__setattr__(
            self,
            "maximum_iterations",
            integer_at_least(
                self.maximum_iterations,
                name="Maximum iterations",
                minimum=1,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "steady", **asdict(self)}


def steady(*, relative_tolerance: float = 1.0e-8, maximum_iterations: int = 500) -> SteadyProcedure:
    return SteadyProcedure(relative_tolerance=relative_tolerance, maximum_iterations=maximum_iterations)
