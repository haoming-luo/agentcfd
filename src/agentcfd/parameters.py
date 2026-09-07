"""Explicit, solver-neutral metadata for editable project factory inputs."""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from typing import Callable, TypeVar


_Factory = TypeVar("_Factory", bound=Callable[..., object])


def _optional_text(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string or None.")
    return value.strip()


def _optional_limit(value: float | None, *, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number or None.")
    selected = float(value)
    if not math.isfinite(selected):
        raise ValueError(f"{name} must be finite.")
    return selected


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """Human/agent presentation metadata; scientific validation stays in APIs."""

    kind: str
    label: str
    description: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    choices: tuple[str, ...] = ()
    nullable: bool = False

    def __post_init__(self) -> None:
        if self.kind not in {"number", "choice"}:
            raise ValueError("Parameter kind must be 'number' or 'choice'.")
        object.__setattr__(self, "label", _optional_text(self.label, name="Label"))
        object.__setattr__(
            self,
            "description",
            _optional_text(self.description, name="Description"),
        )
        object.__setattr__(self, "unit", _optional_text(self.unit, name="Unit"))
        minimum = _optional_limit(self.minimum, name="Minimum")
        maximum = _optional_limit(self.maximum, name="Maximum")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        if minimum is not None and maximum is not None and minimum >= maximum:
            raise ValueError("Parameter minimum must be below maximum.")
        if not isinstance(self.exclusive_minimum, bool) or not isinstance(
            self.nullable, bool
        ):
            raise ValueError("Parameter flags must be booleans.")
        choices = tuple(self.choices)
        if self.kind == "choice":
            if not choices or any(not isinstance(item, str) or not item for item in choices):
                raise ValueError("Choice parameters require non-empty string choices.")
            if len(set(choices)) != len(choices):
                raise ValueError("Parameter choices must be unique.")
        elif choices:
            raise ValueError("Number parameters cannot declare choices.")
        object.__setattr__(self, "choices", choices)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "unit": self.unit,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "exclusive_minimum": self.exclusive_minimum,
            "choices": list(self.choices),
            "nullable": self.nullable,
        }


def number(
    label: str,
    *,
    description: str,
    unit: str | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
    nullable: bool = False,
) -> ParameterSpec:
    """Describe one numeric project input in canonical engineering units."""

    return ParameterSpec(
        kind="number",
        label=label,
        description=description,
        unit=unit,
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
        nullable=nullable,
    )


def choice(
    label: str,
    values: tuple[str, ...],
    *,
    description: str,
    nullable: bool = False,
) -> ParameterSpec:
    """Describe one finite string choice without coupling it to a provider."""

    return ParameterSpec(
        kind="choice",
        label=label,
        description=description,
        choices=values,
        nullable=nullable,
    )


def describe(**specifications: ParameterSpec):
    """Attach explicit UI/agent metadata to a readable ``build()`` factory."""

    if any(not isinstance(spec, ParameterSpec) for spec in specifications.values()):
        raise TypeError("Every described project parameter must be a ParameterSpec.")

    def decorator(factory: _Factory) -> _Factory:
        signature = inspect.signature(factory)
        unknown = sorted(set(specifications) - set(signature.parameters))
        if unknown:
            raise ValueError(
                "Parameter descriptions target unknown factory inputs: "
                + ", ".join(unknown)
                + "."
            )
        setattr(factory, "__agentcfd_parameter_specs__", dict(specifications))
        return factory

    return decorator


__all__ = ["ParameterSpec", "choice", "describe", "number"]
