"""Small runtime validators shared by public engineering inputs."""

from __future__ import annotations

import math
from numbers import Integral


def finite_float(value: object, *, name: str) -> float:
    """Return a finite float and reject booleans and non-numeric values."""

    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        selected = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number.") from error
    if not math.isfinite(selected):
        raise ValueError(f"{name} must be a finite number.")
    return selected


def positive_float(value: object, *, name: str) -> float:
    """Return a finite float strictly greater than zero."""

    selected = finite_float(value, name=name)
    if selected <= 0.0:
        raise ValueError(f"{name} must be positive.")
    return selected


def nonnegative_float(value: object, *, name: str) -> float:
    """Return a finite float greater than or equal to zero."""

    selected = finite_float(value, name=name)
    if selected < 0.0:
        raise ValueError(f"{name} cannot be negative.")
    return selected


def integer_at_least(value: object, *, name: str, minimum: int) -> int:
    """Return an integer at or above a declared lower bound."""

    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be an integer.")
    selected = int(value)
    if selected < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return selected
