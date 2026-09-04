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


@dataclass(frozen=True, slots=True)
class TransientProcedure:
    """Physical-time advancement independent of an OpenFOAM application name."""

    end_time: float
    initial_time_step: float
    maximum_time_step: float | None = None
    maximum_courant_number: float = 0.5
    pressure_velocity_correctors: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "end_time", positive_float(self.end_time, name="End time"))
        object.__setattr__(
            self,
            "initial_time_step",
            positive_float(self.initial_time_step, name="Initial time step"),
        )
        maximum = self.maximum_time_step
        if maximum is None:
            maximum = self.initial_time_step
        maximum = positive_float(maximum, name="Maximum time step")
        if maximum < self.initial_time_step:
            raise ValueError("Maximum time step cannot be smaller than the initial time step.")
        object.__setattr__(self, "maximum_time_step", maximum)
        object.__setattr__(
            self,
            "maximum_courant_number",
            positive_float(
                self.maximum_courant_number,
                name="Maximum Courant number",
            ),
        )
        object.__setattr__(
            self,
            "pressure_velocity_correctors",
            integer_at_least(
                self.pressure_velocity_correctors,
                name="Pressure-velocity correctors",
                minimum=1,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {"type": "transient", **asdict(self)}


def transient(
    *,
    end_time: float,
    initial_time_step: float,
    maximum_time_step: float | None = None,
    maximum_courant_number: float = 0.5,
    pressure_velocity_correctors: int = 2,
) -> TransientProcedure:
    return TransientProcedure(
        end_time=end_time,
        initial_time_step=initial_time_step,
        maximum_time_step=maximum_time_step,
        maximum_courant_number=maximum_courant_number,
        pressure_velocity_correctors=pressure_velocity_correctors,
    )


Procedure = SteadyProcedure | TransientProcedure


__all__ = ["Procedure", "SteadyProcedure", "TransientProcedure", "steady", "transient"]
