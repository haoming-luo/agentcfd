"""Dependency-free zero-dimensional incompressible hydraulic networks.

The public objects in this module model system connectivity and integral
balances.  They do not generate a mesh or pretend to replace a resolved CFD
calculation.  All inputs use SI units.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from ._validation import finite_float, integer_at_least, nonnegative_float, positive_float
from .fluids import NewtonianFluid


def _name(value: object, *, kind: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{kind} name must be a non-empty string.")
    return value.strip()


def _signed_unit(value: float) -> float:
    return 1.0 if value > 0.0 else -1.0 if value < 0.0 else 0.0


def churchill_friction_factor(
    reynolds_number: float,
    *,
    relative_roughness: float = 0.0,
) -> float:
    """Return the Darcy factor from the explicit Churchill all-regime model.

    This named correlation is useful for network iterations that cross the
    laminar-to-turbulent region.  It is a modeling choice, not evidence that a
    transitional three-dimensional flow is accurately represented.
    """

    reynolds = positive_float(reynolds_number, name="reynolds_number")
    roughness = nonnegative_float(relative_roughness, name="relative_roughness")
    if roughness >= 1.0:
        raise ValueError("relative_roughness must be smaller than one.")
    if reynolds < 100.0:
        return 64.0 / reynolds
    base = (7.0 / reynolds) ** 0.9 + 0.27 * roughness
    a_term = (2.457 * math.log(1.0 / base)) ** 16
    b_term = (37530.0 / reynolds) ** 16
    return 8.0 * ((8.0 / reynolds) ** 12 + (a_term + b_term) ** -1.5) ** (
        1.0 / 12.0
    )


@dataclass(frozen=True, slots=True)
class Node:
    """A lumped hydraulic node.

    ``pressure`` is prescribed static gauge pressure in Pa.  A node without a
    pressure is solved.  ``volume_flow_source`` is positive into the network
    and negative for a withdrawal, in m^3/s.
    """

    name: str
    elevation: float = 0.0
    pressure: float | None = None
    volume_flow_source: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, kind="Node"))
        object.__setattr__(
            self,
            "elevation",
            finite_float(self.elevation, name="elevation"),
        )
        if self.pressure is not None:
            object.__setattr__(
                self,
                "pressure",
                finite_float(self.pressure, name="pressure"),
            )
        source = finite_float(self.volume_flow_source, name="volume_flow_source")
        object.__setattr__(self, "volume_flow_source", source)
        if self.pressure is not None and source != 0.0:
            raise ValueError(
                "A prescribed-pressure node cannot also prescribe volume_flow_source."
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "elevation": {"value": self.elevation, "unit": "m"},
            "pressure": (
                None if self.pressure is None else {"value": self.pressure, "unit": "Pa"}
            ),
            "volume_flow_source": {
                "value": self.volume_flow_source,
                "unit": "m^3/s",
                "sign_convention": "positive-into-network",
            },
        }


@dataclass(frozen=True, slots=True)
class CircularPipe:
    """A constant-diameter pipe with distributed and lumped passive loss."""

    name: str
    start_node: str
    end_node: str
    length: float
    diameter: float
    roughness: float = 0.0
    loss_coefficient: float = 0.0
    friction_model: str = "churchill-1977"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, kind="Branch"))
        object.__setattr__(
            self, "start_node", _name(self.start_node, kind="Start node")
        )
        object.__setattr__(self, "end_node", _name(self.end_node, kind="End node"))
        if self.start_node == self.end_node:
            raise ValueError("A branch must connect two different nodes.")
        object.__setattr__(self, "length", positive_float(self.length, name="length"))
        diameter = positive_float(self.diameter, name="diameter")
        object.__setattr__(self, "diameter", diameter)
        roughness = nonnegative_float(self.roughness, name="roughness")
        if roughness >= diameter:
            raise ValueError("roughness must be smaller than diameter.")
        object.__setattr__(self, "roughness", roughness)
        object.__setattr__(
            self,
            "loss_coefficient",
            nonnegative_float(self.loss_coefficient, name="loss_coefficient"),
        )
        if self.friction_model != "churchill-1977":
            raise ValueError("friction_model must be 'churchill-1977'.")

    @property
    def area(self) -> float:
        return math.pi * self.diameter**2 / 4.0

    def pressure_loss(
        self,
        volume_flow_rate: float,
        *,
        fluid: NewtonianFluid,
    ) -> float:
        """Return signed passive pressure loss in the branch direction."""

        _require_fluid(fluid)
        flow = finite_float(volume_flow_rate, name="volume_flow_rate")
        if flow == 0.0:
            return 0.0
        velocity = abs(flow) / self.area
        reynolds = (
            fluid.density * velocity * self.diameter / fluid.dynamic_viscosity
        )
        friction = churchill_friction_factor(
            reynolds,
            relative_roughness=self.roughness / self.diameter,
        )
        magnitude = (
            friction * self.length / self.diameter + self.loss_coefficient
        ) * 0.5 * fluid.density * velocity**2
        return _signed_unit(flow) * magnitude

    def flow_rate(
        self,
        pressure_difference: float,
        *,
        fluid: NewtonianFluid,
        pressure_tolerance: float = 1.0e-7,
    ) -> float:
        """Invert the monotonic branch law for a signed driving pressure."""

        target_signed = finite_float(
            pressure_difference, name="pressure_difference"
        )
        tolerance = positive_float(pressure_tolerance, name="pressure_tolerance")
        if target_signed == 0.0:
            return 0.0
        target = abs(target_signed)
        upper = max(self.area * 1.0e-6, 1.0e-12)
        for _ in range(200):
            if abs(self.pressure_loss(upper, fluid=fluid)) >= target:
                break
            upper *= 2.0
        else:
            raise ValueError(f"Could not bracket flow for pipe {self.name!r}.")
        lower = 0.0
        for _ in range(160):
            middle = 0.5 * (lower + upper)
            loss = abs(self.pressure_loss(middle, fluid=fluid))
            if loss < target:
                lower = middle
            else:
                upper = middle
            if abs(loss - target) <= tolerance:
                return _signed_unit(target_signed) * middle
        return _signed_unit(target_signed) * 0.5 * (lower + upper)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "circular-pipe",
            "name": self.name,
            "start_node": self.start_node,
            "end_node": self.end_node,
            "length": {"value": self.length, "unit": "m"},
            "diameter": {"value": self.diameter, "unit": "m"},
            "roughness": {"value": self.roughness, "unit": "m"},
            "loss_coefficient": self.loss_coefficient,
            "friction_model": self.friction_model,
        }


@dataclass(frozen=True, slots=True)
class FlowResistance:
    """A generic passive law ``dp = R1 Q + R2 |Q| Q``."""

    name: str
    start_node: str
    end_node: str
    linear_resistance: float = 0.0
    quadratic_resistance: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name, kind="Branch"))
        object.__setattr__(
            self, "start_node", _name(self.start_node, kind="Start node")
        )
        object.__setattr__(self, "end_node", _name(self.end_node, kind="End node"))
        if self.start_node == self.end_node:
            raise ValueError("A branch must connect two different nodes.")
        linear = nonnegative_float(
            self.linear_resistance, name="linear_resistance"
        )
        quadratic = nonnegative_float(
            self.quadratic_resistance, name="quadratic_resistance"
        )
        if linear == 0.0 and quadratic == 0.0:
            raise ValueError("At least one resistance coefficient must be positive.")
        object.__setattr__(self, "linear_resistance", linear)
        object.__setattr__(self, "quadratic_resistance", quadratic)

    def pressure_loss(
        self,
        volume_flow_rate: float,
        *,
        fluid: NewtonianFluid,
    ) -> float:
        _require_fluid(fluid)
        flow = finite_float(volume_flow_rate, name="volume_flow_rate")
        return self.linear_resistance * flow + self.quadratic_resistance * abs(flow) * flow

    def flow_rate(
        self,
        pressure_difference: float,
        *,
        fluid: NewtonianFluid,
        pressure_tolerance: float = 1.0e-7,
    ) -> float:
        _require_fluid(fluid)
        positive_float(pressure_tolerance, name="pressure_tolerance")
        signed_pressure = finite_float(
            pressure_difference, name="pressure_difference"
        )
        if signed_pressure == 0.0:
            return 0.0
        target = abs(signed_pressure)
        if self.quadratic_resistance == 0.0:
            magnitude = target / self.linear_resistance
        elif self.linear_resistance == 0.0:
            magnitude = math.sqrt(target / self.quadratic_resistance)
        else:
            magnitude = 2.0 * target / (
                self.linear_resistance
                + math.sqrt(
                    self.linear_resistance**2
                    + 4.0 * self.quadratic_resistance * target
                )
            )
        return _signed_unit(signed_pressure) * magnitude

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "flow-resistance",
            "name": self.name,
            "start_node": self.start_node,
            "end_node": self.end_node,
            "linear_resistance": {
                "value": self.linear_resistance,
                "unit": "Pa*s/m^3",
            },
            "quadratic_resistance": {
                "value": self.quadratic_resistance,
                "unit": "Pa*s^2/m^6",
            },
        }


Branch: TypeAlias = CircularPipe | FlowResistance


def _require_fluid(fluid: object) -> NewtonianFluid:
    if not isinstance(fluid, NewtonianFluid):
        raise TypeError("A zero-dimensional network requires a NewtonianFluid.")
    return fluid


def _solve_dense(matrix: list[list[float]], right: list[float]) -> list[float]:
    size = len(right)
    augmented = [row[:] + [right[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1.0e-24:
            raise ValueError("The zero-dimensional network Jacobian is singular.")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for item in range(column, size + 1):
            augmented[column][item] /= divisor
        for row in range(size):
            if row == column:
                continue
            multiplier = augmented[row][column]
            for item in range(column, size + 1):
                augmented[row][item] -= multiplier * augmented[column][item]
    return [augmented[index][-1] for index in range(size)]


@dataclass(frozen=True, slots=True)
class ZeroDResult:
    """A bounded, JSON-ready zero-dimensional network result."""

    status: str
    converged: bool
    accepted: bool
    trust_level: str
    model_name: str
    model_sha256: str
    iterations: int
    flow_tolerance: float
    pressure_tolerance: float
    maximum_mass_balance_residual: float
    maximum_pressure_closure_residual: float
    nodes: tuple[dict[str, object], ...]
    branches: tuple[dict[str, object], ...]
    checks: tuple[dict[str, object], ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "agentcfd.zero-d-result/0.1",
            "status": self.status,
            "converged": self.converged,
            "accepted": self.accepted,
            "trust_level": self.trust_level,
            "model": {
                "name": self.model_name,
                "sha256": self.model_sha256,
                "physics": "steady-single-phase-incompressible-constant-property",
            },
            "solver": {
                "method": "damped-nodal-newton",
                "iterations": self.iterations,
                "flow_tolerance": {
                    "value": self.flow_tolerance,
                    "unit": "m^3/s",
                },
                "pressure_tolerance": {
                    "value": self.pressure_tolerance,
                    "unit": "Pa",
                },
                "maximum_mass_balance_residual": {
                    "value": self.maximum_mass_balance_residual,
                    "unit": "m^3/s",
                },
                "maximum_pressure_closure_residual": {
                    "value": self.maximum_pressure_closure_residual,
                    "unit": "Pa",
                },
            },
            "nodes": copy.deepcopy(list(self.nodes)),
            "branches": copy.deepcopy(list(self.branches)),
            "checks": copy.deepcopy(list(self.checks)),
            "limitations": list(self.limitations),
        }

    def require_accepted(self) -> "ZeroDResult":
        if not self.accepted:
            failed = [str(item["name"]) for item in self.checks if not item["passed"]]
            raise RuntimeError("Zero-dimensional result is not accepted: " + ", ".join(failed))
        return self

    def node(self, name: str) -> dict[str, object]:
        """Return one node record by name without exposing mutable internals."""

        selected = _name(name, kind="Node")
        for item in self.nodes:
            if item["name"] == selected:
                return copy.deepcopy(item)
        raise KeyError(f"Unknown zero-dimensional node {name!r}.")

    def branch(self, name: str) -> dict[str, object]:
        """Return one branch record by name without exposing mutable internals."""

        selected = _name(name, kind="Branch")
        for item in self.branches:
            if item["name"] == selected:
                return copy.deepcopy(item)
        raise KeyError(f"Unknown zero-dimensional branch {name!r}.")

    def pressure(self, node_name: str) -> float:
        """Return a solved or prescribed node pressure in Pa."""

        return float(self.node(node_name)["pressure"]["value"])

    def volume_flow_rate(self, branch_name: str) -> float:
        """Return signed branch volume flow in m^3/s."""

        return float(self.branch(branch_name)["volume_flow_rate"]["value"])

    def mass_flow_rate(self, branch_name: str) -> float:
        """Return signed branch mass flow in kg/s."""

        return float(self.branch(branch_name)["mass_flow_rate"]["value"])

    def write_json(self, path: str | Path) -> Path:
        """Write the versioned result contract, replacing an older file."""

        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        return target


class Network:
    """A readable builder and solver for a passive hydraulic network."""

    def __init__(
        self,
        name: str,
        *,
        fluid: NewtonianFluid,
        gravity: float = 9.80665,
    ) -> None:
        self.name = _name(name, kind="Network")
        self.fluid = _require_fluid(fluid)
        self.gravity = nonnegative_float(gravity, name="gravity")
        self._nodes: dict[str, Node] = {}
        self._branches: dict[str, Branch] = {}

    def node(
        self,
        name: str,
        *,
        elevation: float = 0.0,
        pressure: float | None = None,
        volume_flow_source: float | None = None,
        mass_flow_source: float | None = None,
    ) -> "Network":
        if volume_flow_source is not None and mass_flow_source is not None:
            raise ValueError(
                "Specify either volume_flow_source or mass_flow_source, not both."
            )
        source = (
            finite_float(mass_flow_source, name="mass_flow_source")
            / self.fluid.density
            if mass_flow_source is not None
            else 0.0
            if volume_flow_source is None
            else volume_flow_source
        )
        item = Node(
            name,
            elevation=elevation,
            pressure=pressure,
            volume_flow_source=source,
        )
        if item.name in self._nodes:
            raise ValueError(f"Node {item.name!r} is already defined.")
        self._nodes[item.name] = item
        return self

    def pipe(
        self,
        name: str,
        start_node: str,
        end_node: str,
        *,
        length: float,
        diameter: float,
        roughness: float = 0.0,
        loss_coefficient: float = 0.0,
    ) -> "Network":
        return self.add_branch(
            CircularPipe(
                name,
                start_node,
                end_node,
                length=length,
                diameter=diameter,
                roughness=roughness,
                loss_coefficient=loss_coefficient,
            )
        )

    def resistance(
        self,
        name: str,
        start_node: str,
        end_node: str,
        *,
        linear_resistance: float = 0.0,
        quadratic_resistance: float = 0.0,
    ) -> "Network":
        return self.add_branch(
            FlowResistance(
                name,
                start_node,
                end_node,
                linear_resistance=linear_resistance,
                quadratic_resistance=quadratic_resistance,
            )
        )

    def add_branch(self, branch: Branch) -> "Network":
        if not isinstance(branch, (CircularPipe, FlowResistance)):
            raise TypeError("Network branches must be CircularPipe or FlowResistance.")
        if branch.name in self._branches:
            raise ValueError(f"Branch {branch.name!r} is already defined.")
        self._branches[branch.name] = branch
        return self

    @property
    def nodes(self) -> tuple[Node, ...]:
        return tuple(self._nodes[name] for name in sorted(self._nodes))

    @property
    def branches(self) -> tuple[Branch, ...]:
        return tuple(self._branches[name] for name in sorted(self._branches))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "agentcfd.zero-d-network/0.1",
            "name": self.name,
            "physics": "steady-single-phase-incompressible-constant-property",
            "fluid": self.fluid.to_dict(),
            "gravity": {"value": self.gravity, "unit": "m/s^2"},
            "nodes": [item.to_dict() for item in self.nodes],
            "branches": [item.to_dict() for item in self.branches],
            "assumptions": [
                "lumped one-dimensional branch losses",
                "uniform constant fluid properties",
                "no storage, compressibility, heat transfer, pumps, or active controls",
            ],
        }

    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def write_json(self, path: str | Path) -> Path:
        """Write the declarative network definition, replacing an older file."""

        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        return target

    def _validate_topology(self) -> None:
        if not self._nodes:
            raise ValueError("A network requires at least one node.")
        if not self._branches:
            raise ValueError("A network requires at least one branch.")
        adjacency = {name: set() for name in self._nodes}
        for branch in self._branches.values():
            missing = [
                name
                for name in (branch.start_node, branch.end_node)
                if name not in self._nodes
            ]
            if missing:
                raise ValueError(
                    f"Branch {branch.name!r} references unknown nodes: "
                    + ", ".join(missing)
                )
            adjacency[branch.start_node].add(branch.end_node)
            adjacency[branch.end_node].add(branch.start_node)
        isolated = sorted(name for name, neighbours in adjacency.items() if not neighbours)
        if isolated:
            raise ValueError("Network contains isolated nodes: " + ", ".join(isolated))
        remaining = set(self._nodes)
        while remaining:
            start = next(iter(remaining))
            component = {start}
            frontier = [start]
            while frontier:
                current = frontier.pop()
                for neighbour in adjacency[current]:
                    if neighbour not in component:
                        component.add(neighbour)
                        frontier.append(neighbour)
            remaining -= component
            if not any(self._nodes[name].pressure is not None for name in component):
                raise ValueError(
                    "Every connected network component requires a prescribed-pressure "
                    "reference; missing for: " + ", ".join(sorted(component))
                )

    def _branch_flows(
        self,
        pressures: dict[str, float],
        *,
        pressure_tolerance: float,
    ) -> dict[str, float]:
        flows = {}
        density = self.fluid.density
        inversion_tolerance = min(pressure_tolerance, 1.0e-10)
        for branch in self.branches:
            start = self._nodes[branch.start_node]
            end = self._nodes[branch.end_node]
            driving = (
                pressures[start.name]
                - pressures[end.name]
                + density * self.gravity * (start.elevation - end.elevation)
            )
            flows[branch.name] = branch.flow_rate(
                driving,
                fluid=self.fluid,
                pressure_tolerance=inversion_tolerance,
            )
        return flows

    def _balances(self, flows: dict[str, float]) -> dict[str, float]:
        balances = {
            name: node.volume_flow_source for name, node in self._nodes.items()
        }
        for branch in self.branches:
            flow = flows[branch.name]
            balances[branch.start_node] -= flow
            balances[branch.end_node] += flow
        return balances

    def solve(
        self,
        *,
        flow_tolerance: float = 1.0e-10,
        pressure_tolerance: float = 1.0e-6,
        maximum_iterations: int = 80,
    ) -> ZeroDResult:
        """Solve free node pressures and branch flows by damped nodal Newton."""

        self._validate_topology()
        selected_flow_tolerance = positive_float(
            flow_tolerance, name="flow_tolerance"
        )
        selected_pressure_tolerance = positive_float(
            pressure_tolerance, name="pressure_tolerance"
        )
        selected_maximum = integer_at_least(
            maximum_iterations, name="maximum_iterations", minimum=1
        )
        fixed = {
            name: node.pressure
            for name, node in self._nodes.items()
            if node.pressure is not None
        }
        free_names = sorted(set(self._nodes) - set(fixed))
        density = self.fluid.density
        mean_total_pressure = sum(
            float(value) + density * self.gravity * self._nodes[name].elevation
            for name, value in fixed.items()
        ) / len(fixed)
        pressures = {
            name: (
                float(node.pressure)
                if node.pressure is not None
                else mean_total_pressure - density * self.gravity * node.elevation
            )
            for name, node in self._nodes.items()
        }

        iterations = 0
        converged = False
        for iterations in range(selected_maximum + 1):
            flows = self._branch_flows(
                pressures, pressure_tolerance=selected_pressure_tolerance
            )
            balances = self._balances(flows)
            residual = [balances[name] for name in free_names]
            norm = max((abs(value) for value in residual), default=0.0)
            if norm <= selected_flow_tolerance:
                converged = True
                break
            if iterations == selected_maximum:
                break
            jacobian = [[0.0 for _ in free_names] for _ in free_names]
            free_index = {name: index for index, name in enumerate(free_names)}
            for branch in self.branches:
                start = self._nodes[branch.start_node]
                end = self._nodes[branch.end_node]
                driving = (
                    pressures[start.name]
                    - pressures[end.name]
                    + density * self.gravity * (start.elevation - end.elevation)
                )
                increment = max(
                    1.0,
                    abs(driving) * 1.0e-5,
                    selected_pressure_tolerance * 10.0,
                )
                plus = branch.flow_rate(
                    driving + increment,
                    fluid=self.fluid,
                    pressure_tolerance=selected_pressure_tolerance,
                )
                minus = branch.flow_rate(
                    driving - increment,
                    fluid=self.fluid,
                    pressure_tolerance=selected_pressure_tolerance,
                )
                conductance = (plus - minus) / (2.0 * increment)
                start_index = free_index.get(start.name)
                end_index = free_index.get(end.name)
                if start_index is not None:
                    jacobian[start_index][start_index] -= conductance
                    if end_index is not None:
                        jacobian[start_index][end_index] += conductance
                if end_index is not None:
                    jacobian[end_index][end_index] -= conductance
                    if start_index is not None:
                        jacobian[end_index][start_index] += conductance
            update = _solve_dense(jacobian, [-value for value in residual])
            accepted_step = False
            damping = 1.0
            for _ in range(24):
                candidate = dict(pressures)
                for index, name in enumerate(free_names):
                    candidate[name] += damping * update[index]
                candidate_flows = self._branch_flows(
                    candidate, pressure_tolerance=selected_pressure_tolerance
                )
                candidate_balances = self._balances(candidate_flows)
                candidate_norm = max(
                    abs(candidate_balances[name]) for name in free_names
                )
                if candidate_norm < norm:
                    pressures = candidate
                    accepted_step = True
                    break
                damping *= 0.5
            if not accepted_step:
                break

        flows = self._branch_flows(
            pressures, pressure_tolerance=selected_pressure_tolerance
        )
        balances = self._balances(flows)
        maximum_balance = max(
            (abs(balances[name]) for name in free_names), default=0.0
        )
        node_records = []
        for node in self.nodes:
            required_boundary_flow = (
                -balances[node.name] if node.pressure is not None else None
            )
            node_records.append(
                {
                    "name": node.name,
                    "boundary_type": (
                        "prescribed-pressure" if node.pressure is not None else "solved-pressure"
                    ),
                    "pressure": {"value": pressures[node.name], "unit": "Pa"},
                    "elevation": {"value": node.elevation, "unit": "m"},
                    "volume_flow_source": {
                        "value": node.volume_flow_source,
                        "unit": "m^3/s",
                    },
                    "mass_balance_residual": {
                        "value": balances[node.name],
                        "unit": "m^3/s",
                    },
                    "required_boundary_volume_flow_rate": (
                        None
                        if required_boundary_flow is None
                        else {"value": required_boundary_flow, "unit": "m^3/s"}
                    ),
                }
            )

        branch_records = []
        maximum_pressure_closure = 0.0
        for branch in self.branches:
            start = self._nodes[branch.start_node]
            end = self._nodes[branch.end_node]
            flow = flows[branch.name]
            static_difference = pressures[start.name] - pressures[end.name]
            elevation_difference = density * self.gravity * (
                start.elevation - end.elevation
            )
            driving = static_difference + elevation_difference
            modeled_loss = branch.pressure_loss(flow, fluid=self.fluid)
            closure = driving - modeled_loss
            maximum_pressure_closure = max(maximum_pressure_closure, abs(closure))
            record: dict[str, object] = {
                "name": branch.name,
                "type": "circular-pipe" if isinstance(branch, CircularPipe) else "flow-resistance",
                "start_node": branch.start_node,
                "end_node": branch.end_node,
                "volume_flow_rate": {"value": flow, "unit": "m^3/s"},
                "mass_flow_rate": {
                    "value": density * flow,
                    "unit": "kg/s",
                },
                "static_pressure_difference": {
                    "value": static_difference,
                    "unit": "Pa",
                },
                "elevation_pressure_difference": {
                    "value": elevation_difference,
                    "unit": "Pa",
                },
                "driving_pressure_difference": {
                    "value": driving,
                    "unit": "Pa",
                },
                "modeled_pressure_loss": {"value": modeled_loss, "unit": "Pa"},
                "pressure_closure_residual": {"value": closure, "unit": "Pa"},
            }
            if isinstance(branch, CircularPipe):
                speed = abs(flow) / branch.area
                reynolds = (
                    density * speed * branch.diameter / self.fluid.dynamic_viscosity
                )
                record.update(
                    {
                        "mean_speed": {"value": speed, "unit": "m/s"},
                        "reynolds_number": reynolds,
                        "flow_regime": (
                            "stagnant"
                            if reynolds == 0.0
                            else "laminar"
                            if reynolds < 2300.0
                            else "transitional"
                            if reynolds < 4000.0
                            else "turbulent"
                        ),
                        "darcy_friction_factor": (
                            None
                            if reynolds == 0.0
                            else churchill_friction_factor(
                                reynolds,
                                relative_roughness=branch.roughness / branch.diameter,
                            )
                        ),
                        "friction_model": branch.friction_model,
                    }
                )
            branch_records.append(record)

        mass_passed = converged and maximum_balance <= selected_flow_tolerance
        pressure_passed = (
            converged
            and maximum_pressure_closure <= selected_pressure_tolerance
        )
        accepted = mass_passed and pressure_passed
        checks = (
            {
                "name": "free-node-volume-conservation",
                "passed": mass_passed,
                "value": maximum_balance,
                "limit": selected_flow_tolerance,
                "unit": "m^3/s",
            },
            {
                "name": "branch-pressure-closure",
                "passed": pressure_passed,
                "value": maximum_pressure_closure,
                "limit": selected_pressure_tolerance,
                "unit": "Pa",
            },
        )
        return ZeroDResult(
            status="completed",
            converged=converged,
            accepted=accepted,
            trust_level="computed",
            model_name=self.name,
            model_sha256=self.fingerprint(),
            iterations=iterations,
            flow_tolerance=selected_flow_tolerance,
            pressure_tolerance=selected_pressure_tolerance,
            maximum_mass_balance_residual=maximum_balance,
            maximum_pressure_closure_residual=maximum_pressure_closure,
            nodes=tuple(node_records),
            branches=tuple(branch_records),
            checks=checks,
            limitations=(
                "Steady, single-phase, incompressible, constant-property flow only.",
                "Churchill friction is a system correlation, not three-dimensional validation.",
                "Pumps, active controls, heat transfer, compressibility, storage, and transients are unsupported.",
            ),
        )


def network(
    name: str,
    *,
    fluid: NewtonianFluid,
    gravity: float = 9.80665,
) -> Network:
    """Create a zero-dimensional hydraulic network using SI units."""

    return Network(name, fluid=fluid, gravity=gravity)


__all__ = [
    "CircularPipe",
    "FlowResistance",
    "Network",
    "Node",
    "ZeroDResult",
    "churchill_friction_factor",
    "network",
]
