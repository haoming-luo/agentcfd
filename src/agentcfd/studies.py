"""Physical study declarations, independent of numerical providers."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Study:
    family: str
    steady: bool
    compressible: bool
    energy: bool
    reacting: bool
    turbulence: str | None = None

    def __post_init__(self) -> None:
        for name in ("steady", "compressible", "energy", "reacting"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"Study {name} must be a boolean.")
        if self.family != "internal-flow":
            raise ValueError(f"Unsupported study family: {self.family!r}")
        if self.reacting and not self.energy:
            raise ValueError("Reacting flow requires the energy equation.")
        allowed = {None, "k-epsilon", "k-omega-sst"}
        if self.turbulence not in allowed:
            raise ValueError(f"turbulence must be one of {sorted(v for v in allowed if v)} or None")

    @property
    def laminar(self) -> bool:
        return self.turbulence is None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def internal_flow(
    *,
    steady: bool = True,
    compressible: bool = False,
    energy: bool = False,
    reacting: bool = False,
    turbulence: str | None = None,
) -> Study:
    """Describe flow inside pipes, ducts, valves, or connected equipment."""

    return Study(
        family="internal-flow",
        steady=steady,
        compressible=compressible,
        energy=energy,
        reacting=reacting,
        turbulence=turbulence,
    )
