"""Provider contracts isolate scientific intent from numerical backends."""

from __future__ import annotations

import math
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Mapping, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..model import Step
    from ..results import SimulationResult


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    name: str
    version: str
    license: str
    available: bool
    execution_boundary: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """One inspectable project-level provider option."""

    name: str
    value_type: str
    effect: str
    description: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("Provider option names must use letters, numbers, or underscores.")
        if self.value_type not in {"boolean", "integer", "number", "string"}:
            raise ValueError("Provider option value_type is unsupported.")
        if self.effect not in {"execution", "mesh", "output", "retention"}:
            raise ValueError("Provider option effect is unsupported.")
        if not self.description.strip():
            raise ValueError("Provider option descriptions cannot be empty.")

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value_type": self.value_type,
            "effect": self.effect,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ProviderOptionContract:
    """Stable validation and discovery contract for one provider family."""

    options: tuple[ProviderOption, ...] = ()

    def __post_init__(self) -> None:
        names = tuple(option.name for option in self.options)
        if len(names) != len(set(names)):
            raise ValueError("Provider option names must be unique.")

    @property
    def accepted(self) -> tuple[str, ...]:
        return tuple(option.name for option in self.options)

    def issues(self, values: Mapping[str, object]) -> tuple[dict[str, object], ...]:
        """Return stable issues without mutating or coercing configuration."""

        known = {option.name: option for option in self.options}
        issues: list[dict[str, object]] = []
        for name in sorted(values):
            option = known.get(name)
            if option is None:
                issues.append(
                    {
                        "code": "ACFD-PROVIDER-OPTION-001",
                        "option": name,
                        "message": f"Unsupported provider option {name!r}.",
                        "suggestions": list(
                            get_close_matches(name, self.accepted, n=3, cutoff=0.58)
                        ),
                    }
                )
                continue
            if not _matches_type(values[name], option.value_type):
                issues.append(
                    {
                        "code": "ACFD-PROVIDER-OPTION-002",
                        "option": name,
                        "message": (
                            f"Provider option {name!r} must be "
                            f"{option.value_type}."
                        ),
                        "suggestions": [],
                    }
                )
        return tuple(issues)

    def validate(self, values: Mapping[str, object], *, provider: str) -> None:
        issues = self.issues(values)
        if issues:
            raise ValueError(
                f"Provider {provider!r} configuration is invalid: "
                + " ".join(
                    f"[{issue['code']}] {issue['message']}" for issue in issues
                )
                + f" Accepted options are {self.accepted!r}."
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": list(self.accepted),
            "options": [option.to_dict() for option in self.options],
        }


def _matches_type(value: object, value_type: str) -> bool:
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    return isinstance(value, str) and bool(value.strip())


class Provider(Protocol):
    def descriptor(self) -> ProviderDescriptor: ...

    def validate(self, step: "Step") -> None: ...

    def run(self, step: "Step") -> "SimulationResult": ...
