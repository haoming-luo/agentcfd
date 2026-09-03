"""OpenFOAM case lowering and external-process execution.

AgentCFD does not import, link, vendor, or modify OpenFOAM.  This provider
creates ordinary OpenFOAM case files and, when explicitly asked to run, calls
an installation already present on the user's machine.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import boundaries
from .._version import __version__
from ..errors import ProviderUnavailableError, UnsupportedCaseError
from ..results import Artifact, Check, Quantity, SimulationResult
from .base import ProviderDescriptor


_FOAM_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _solver_converged(log: str) -> bool:
    """Return true only for an explicit OpenFOAM convergence statement."""

    return bool(re.search(r"(?mi)^.*solution converged in \d+ iterations\s*$", log))


@dataclass(frozen=True, slots=True)
class OpenFOAMMeshControls:
    """Provider-specific controls for the first circular-pipe mesh."""

    cross_section_cells: int = 8
    axial_cells: int | None = None

    def __post_init__(self) -> None:
        if self.cross_section_cells < 2:
            raise ValueError("cross_section_cells must be at least 2.")
        if self.axial_cells is not None and self.axial_cells < 2:
            raise ValueError("axial_cells must be at least 2 when supplied.")


@dataclass(frozen=True, slots=True)
class PreparedOpenFOAMCase:
    """Content-addressed record of a generated OpenFOAM case."""

    directory: Path
    model_sha256: str
    case_sha256: str
    files: dict[str, str]
    capability: str = "openfoam.steady-laminar-circular-pipe"
    schema: str = "agentcfd.openfoam-case/0.1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "capability": self.capability,
            "model_sha256": self.model_sha256,
            "case_sha256": self.case_sha256,
            "files": dict(sorted(self.files.items())),
            "provider": {
                "name": "openfoam",
                "execution_boundary": "filesystem-and-subprocess",
                "license": "GPL-3.0-or-later (external program)",
            },
        }


class OpenFOAMProvider:
    """Lower a bounded model to an external ``simpleFoam`` workflow.

    The supported case is intentionally narrow: steady, incompressible,
    isothermal, Newtonian, laminar flow through a smooth circular pipe.  Case
    generation is deterministic and testable without an OpenFOAM installation.
    Execution additionally requires ``blockMesh`` and ``simpleFoam`` on PATH.
    """

    def __init__(
        self,
        *,
        case_directory: str | Path | None = None,
        mesh: OpenFOAMMeshControls | None = None,
        timeout_seconds: float = 3600.0,
    ) -> None:
        if timeout_seconds <= 0.0:
            raise ValueError("timeout_seconds must be positive.")
        self.case_directory = Path(case_directory) if case_directory is not None else None
        self.mesh = mesh or OpenFOAMMeshControls()
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _commands() -> dict[str, str | None]:
        return {
            "blockMesh": shutil.which("blockMesh"),
            "simpleFoam": shutil.which("simpleFoam"),
        }

    def descriptor(self) -> ProviderDescriptor:
        commands = self._commands()
        available = all(commands.values())
        version = os.environ.get("WM_PROJECT_VERSION", "externally-managed")
        return ProviderDescriptor(
            name="openfoam",
            version=version,
            license="GPL-3.0-or-later (external program)",
            available=available,
            execution_boundary="filesystem-and-subprocess",
            capabilities=("openfoam.steady-laminar-circular-pipe",),
        )

    def prepare(self, step, directory: str | Path | None = None) -> PreparedOpenFOAMCase:
        """Validate and write a deterministic OpenFOAM case.

        The destination must not already contain files.  AgentCFD never deletes
        or silently overwrites an existing CFD case.
        """

        step.model.validate()
        self._validate_supported(step)
        target = Path(directory) if directory is not None else self.case_directory
        if target is None:
            raise ValueError("OpenFOAM case_directory is required for prepare or run.")
        if target.exists() and any(target.iterdir()):
            raise FileExistsError(f"OpenFOAM case directory is not empty: {target}")
        rendered = self._render_files(step)
        target.mkdir(parents=True, exist_ok=True)

        hashes: dict[str, str] = {}
        for relative, content in sorted(rendered.items()):
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            hashes[relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()

        case_identity = hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prepared = PreparedOpenFOAMCase(
            directory=target,
            model_sha256=step.model.fingerprint(),
            case_sha256=case_identity,
            files=hashes,
        )
        manifest = target / "agentcfd-case.json"
        manifest.write_text(
            json.dumps(prepared.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return prepared

    def run(self, step) -> SimulationResult:
        """Prepare and execute the case through external OpenFOAM commands.

        The initial result recovery is deliberately conservative. A successful
        process is reported as completed execution evidence. Numerical
        convergence additionally requires OpenFOAM's explicit convergence
        marker, and scientific acceptance remains blocked until mesh-field
        conservation and pressure-loss recovery are implemented and checked.
        """

        prepared = self.prepare(step)
        commands = self._commands()
        missing = [name for name, path in commands.items() if path is None]
        if missing:
            raise ProviderUnavailableError(
                "OpenFOAM execution requires commands on PATH: " + ", ".join(missing)
            )

        logs: dict[str, str] = {}
        return_codes: dict[str, int] = {}
        for name in ("blockMesh", "simpleFoam"):
            try:
                completed = subprocess.run(
                    [str(commands[name]), "-case", str(prepared.directory)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
                combined = completed.stdout + completed.stderr
                return_codes[name] = completed.returncode
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
                stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else (error.stderr or "")
                combined = stdout + stderr + f"\nAgentCFD timeout after {self.timeout_seconds:g} seconds.\n"
                return_codes[name] = -124
            logs[name] = combined
            (prepared.directory / f"log.{name}").write_text(combined, encoding="utf-8")
            if return_codes[name] != 0:
                break

        process_ok = return_codes.get("blockMesh") == 0 and return_codes.get("simpleFoam") == 0
        solver_log = logs.get("simpleFoam", "")
        reached_end = process_ok and bool(re.search(r"(?m)^End\s*$", solver_log))
        solver_converged = process_ok and _solver_converged(solver_log)
        reference_drop = self._reference_pressure_drop(step)
        artifact_paths = {
            "case_manifest": prepared.directory / "agentcfd-case.json",
            **{
                f"log_{name}": prepared.directory / f"log.{name}"
                for name in logs
            },
        }
        return SimulationResult(
            status="completed" if process_ok else "failed",
            converged=solver_converged,
            provider="openfoam",
            quantities={
                "reference.flow.pressure_drop": Quantity(reference_drop, "Pa"),
            },
            checks=(
                Check(
                    name="openfoam-process",
                    passed=process_ok,
                    value=json.dumps(return_codes, sort_keys=True),
                    limit="all return codes equal zero",
                    kind="runtime",
                    observable="provider.process",
                ),
                Check(
                    name="solver-end-marker",
                    passed=reached_end,
                    value="found" if reached_end else "missing",
                    limit="OpenFOAM log ends with End",
                    kind="runtime",
                    observable="provider.convergence_marker",
                ),
                Check(
                    name="solver-convergence-marker",
                    passed=solver_converged,
                    value="found" if solver_converged else "missing",
                    limit="OpenFOAM reports solution converged",
                    message=(
                        "A normal End marker proves process completion, not numerical "
                        "convergence; reaching the configured iteration limit remains "
                        "unconverged."
                    ),
                    kind="verification",
                    observable="provider.numerical_convergence",
                ),
                Check(
                    name="field-conservation-recovery",
                    passed=False,
                    value="not-implemented",
                    limit="required before scientific acceptance",
                    message=(
                        "The experimental provider does not yet recover mesh-field mass balance "
                        "and pressure loss; execution is therefore not an accepted CFD result."
                    ),
                    kind="verification",
                    observable="flow.mass_balance_and_pressure_drop",
                ),
            ),
            artifacts={
                name: Artifact.from_path(path, role="execution-evidence", media_type="text/plain")
                for name, path in artifact_paths.items()
            },
            scientific_inputs={
                "model": step.model.to_dict(),
                "procedure": step.procedure.to_dict(),
                "output_request": step.output.to_dict(),
                "lowered_case_sha256": prepared.case_sha256,
            },
            provenance={
                "agentcfd_version": __version__,
                "model_sha256": step.model.fingerprint(),
                "case_sha256": prepared.case_sha256,
                "provider": "openfoam",
                "provider_version": self.descriptor().version,
                "execution_boundary": "filesystem-and-subprocess",
                "case_manifest": str(prepared.directory / "agentcfd-case.json"),
            },
            messages=(
                "OpenFOAM execution evidence is experimental and is not yet scientific acceptance.",
            ),
        )
    def _validate_supported(self, step) -> None:
        model = step.model
        study = model.study
        if not study.steady or study.compressible or study.energy or study.reacting or not study.laminar:
            raise UnsupportedCaseError(
                "The initial OpenFOAM provider supports steady, incompressible, "
                "isothermal, Newtonian, laminar internal flow only."
            )
        if model.domain.roughness != 0.0:
            raise UnsupportedCaseError("The initial OpenFOAM provider requires a smooth pipe.")
        wall_conditions = [
            value
            for value in model.boundary_conditions.values()
            if isinstance(value, boundaries.NoSlipWall)
        ]
        if any(condition.roughness not in (None, 0.0) for condition in wall_conditions):
            raise UnsupportedCaseError("Rough-wall lowering is not implemented.")
        if len(wall_conditions) != 1:
            raise UnsupportedCaseError("The initial OpenFOAM pipe mesh requires exactly one wall boundary.")
        reynolds = (
            model.fluid.density
            * self._mean_velocity(model)
            * model.domain.diameter
            / model.fluid.dynamic_viscosity
        )
        if reynolds >= 2300.0:
            raise UnsupportedCaseError(
                f"Re={reynolds:.6g} is outside the declared laminar provider range Re < 2300."
            )
        for name in model.boundary_conditions:
            if not _FOAM_WORD.fullmatch(name):
                raise UnsupportedCaseError(
                    f"Boundary name {name!r} is not a valid OpenFOAM word; use letters, digits, and underscores."
                )

    def _render_files(self, step) -> dict[str, str]:
        model = step.model
        inlet_name, inlet = next(
            (name, value)
            for name, value in model.boundary_conditions.items()
            if isinstance(value, (boundaries.MassFlowInlet, boundaries.MeanVelocityInlet))
        )
        outlet_name, outlet = next(
            (name, value)
            for name, value in model.boundary_conditions.items()
            if isinstance(value, boundaries.PressureOutlet)
        )
        wall_names = tuple(
            name
            for name, value in model.boundary_conditions.items()
            if isinstance(value, boundaries.NoSlipWall)
        )
        rho = model.fluid.density
        mean_velocity = self._mean_velocity(model)
        outlet_kinematic_pressure = outlet.gauge_pressure / rho

        axial_cells = self.mesh.axial_cells
        if axial_cells is None:
            axial_cells = max(20, min(800, math.ceil(2.0 * model.domain.length / model.domain.diameter)))

        return {
            "0/U": _velocity_field(inlet_name, outlet_name, wall_names, mean_velocity),
            "0/p": _pressure_field(inlet_name, outlet_name, wall_names, outlet_kinematic_pressure),
            "constant/transportProperties": _transport_properties(model.fluid.kinematic_viscosity),
            "constant/turbulenceProperties": _turbulence_properties(),
            "system/blockMeshDict": _block_mesh_dict(
                length=model.domain.length,
                radius=model.domain.diameter / 2.0,
                cross_cells=self.mesh.cross_section_cells,
                axial_cells=axial_cells,
                inlet=inlet_name,
                outlet=outlet_name,
                walls=wall_names,
            ),
            "system/controlDict": _control_dict(step.procedure.maximum_iterations),
            "system/fvSchemes": _fv_schemes(),
            "system/fvSolution": _fv_solution(step.procedure.relative_tolerance),
        }

    @staticmethod
    def _reference_pressure_drop(step) -> float:
        model = step.model
        inlet = next(
            value
            for value in model.boundary_conditions.values()
            if isinstance(value, (boundaries.MassFlowInlet, boundaries.MeanVelocityInlet))
        )
        if isinstance(inlet, boundaries.MassFlowInlet):
            velocity = inlet.mass_flow_rate / (model.fluid.density * model.domain.area)
        else:
            velocity = inlet.velocity
        return (
            32.0
            * model.fluid.dynamic_viscosity
            * model.domain.length
            * velocity
            / model.domain.diameter**2
        )

    @staticmethod
    def _mean_velocity(model) -> float:
        inlet = next(
            value
            for value in model.boundary_conditions.values()
            if isinstance(value, (boundaries.MassFlowInlet, boundaries.MeanVelocityInlet))
        )
        if isinstance(inlet, boundaries.MassFlowInlet):
            return inlet.mass_flow_rate / (model.fluid.density * model.domain.area)
        return inlet.velocity


def _header(*, object_name: str, class_name: str, location: str | None = None) -> str:
    location_line = f'    location    "{location}";\n' if location is not None else ""
    return (
        "/* Generated by AgentCFD. OpenFOAM is an external GPL program. */\n"
        "FoamFile\n{\n"
        "    version     2.0;\n"
        "    format      ascii;\n"
        f"    class       {class_name};\n"
        f"{location_line}"
        f"    object      {object_name};\n"
        "}\n\n"
    )


def _velocity_field(inlet: str, outlet: str, walls: tuple[str, ...], velocity: float) -> str:
    wall_blocks = "\n".join(
        f"    {name}\n    {{\n        type noSlip;\n    }}" for name in walls
    )
    return _header(object_name="U", class_name="volVectorField", location="0") + f"""dimensions      [0 1 -1 0 0 0 0];
internalField   uniform (0 0 {velocity:.17g});
boundaryField
{{
    {inlet}
    {{
        type fixedValue;
        value uniform (0 0 {velocity:.17g});
    }}
    {outlet}
    {{
        type zeroGradient;
    }}
{wall_blocks}
}}
"""


def _pressure_field(inlet: str, outlet: str, walls: tuple[str, ...], pressure: float) -> str:
    wall_blocks = "\n".join(
        f"    {name}\n    {{\n        type zeroGradient;\n    }}" for name in walls
    )
    return _header(object_name="p", class_name="volScalarField", location="0") + f"""dimensions      [0 2 -2 0 0 0 0];
internalField   uniform {pressure:.17g};
boundaryField
{{
    {inlet}
    {{
        type zeroGradient;
    }}
    {outlet}
    {{
        type fixedValue;
        value uniform {pressure:.17g};
    }}
{wall_blocks}
}}
"""


def _transport_properties(nu: float) -> str:
    return _header(object_name="transportProperties", class_name="dictionary", location="constant") + f"""transportModel Newtonian;
nu              [0 2 -1 0 0 0 0] {nu:.17g};
"""


def _turbulence_properties() -> str:
    return _header(object_name="turbulenceProperties", class_name="dictionary", location="constant") + """simulationType laminar;
"""


def _block_mesh_dict(
    *,
    length: float,
    radius: float,
    cross_cells: int,
    axial_cells: int,
    inlet: str,
    outlet: str,
    walls: tuple[str, ...],
) -> str:
    inner = radius / 3.0
    wall_name = walls[0]
    if len(walls) != 1:
        raise UnsupportedCaseError("The initial OpenFOAM pipe mesh requires exactly one wall boundary.")
    return _header(object_name="blockMeshDict", class_name="dictionary", location="system") + f"""convertToMeters 1;

geometry
{{
    pipeCylinder
    {{
        type cylinder;
        point1 (0 0 0);
        point2 (0 0 {length:.17g});
        radius {radius:.17g};
    }}
}}

vertices
(
    ({-inner:.17g} {-inner:.17g} 0)
    ({inner:.17g} {-inner:.17g} 0)
    ({-inner:.17g} {inner:.17g} 0)
    ({inner:.17g} {inner:.17g} 0)
    project ({-radius:.17g} {-radius:.17g} 0) (pipeCylinder)
    project ({radius:.17g} {-radius:.17g} 0) (pipeCylinder)
    project ({-radius:.17g} {radius:.17g} 0) (pipeCylinder)
    project ({radius:.17g} {radius:.17g} 0) (pipeCylinder)
    ({-inner:.17g} {-inner:.17g} {length:.17g})
    ({inner:.17g} {-inner:.17g} {length:.17g})
    ({-inner:.17g} {inner:.17g} {length:.17g})
    ({inner:.17g} {inner:.17g} {length:.17g})
    project ({-radius:.17g} {-radius:.17g} {length:.17g}) (pipeCylinder)
    project ({radius:.17g} {-radius:.17g} {length:.17g}) (pipeCylinder)
    project ({-radius:.17g} {radius:.17g} {length:.17g}) (pipeCylinder)
    project ({radius:.17g} {radius:.17g} {length:.17g}) (pipeCylinder)
);

blocks
(
    hex (4 5 1 0 12 13 9 8) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
    hex (4 0 2 6 12 8 10 14) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
    hex (1 5 7 3 9 13 15 11) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
    hex (2 3 7 6 10 11 15 14) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
    hex (0 1 3 2 8 9 11 10) ({cross_cells} {cross_cells} {axial_cells}) simpleGrading (1 1 1)
);

edges
(
    arc 4 5 (0 {-radius:.17g} 0)
    arc 7 5 ({radius:.17g} 0 0)
    arc 6 7 (0 {radius:.17g} 0)
    arc 4 6 ({-radius:.17g} 0 0)
    arc 12 13 (0 {-radius:.17g} {length:.17g})
    arc 13 15 ({radius:.17g} 0 {length:.17g})
    arc 12 14 ({-radius:.17g} 0 {length:.17g})
    arc 14 15 (0 {radius:.17g} {length:.17g})
);

boundary
(
    {inlet}
    {{
        type patch;
        faces
        (
            (4 0 1 5)
            (4 6 2 0)
            (1 3 7 5)
            (2 6 7 3)
            (0 2 3 1)
        );
    }}
    {outlet}
    {{
        type patch;
        faces
        (
            (12 13 9 8)
            (12 8 10 14)
            (9 13 15 11)
            (10 11 15 14)
            (8 9 11 10)
        );
    }}
    {wall_name}
    {{
        type wall;
        faces
        (
            (4 5 13 12)
            (4 12 14 6)
            (5 7 15 13)
            (6 14 15 7)
        );
    }}
);
"""


def _control_dict(maximum_iterations: int) -> str:
    return _header(object_name="controlDict", class_name="dictionary", location="system") + f"""application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {maximum_iterations};
deltaT          1;
writeControl    timeStep;
writeInterval   {maximum_iterations};
purgeWrite      1;
writeFormat     ascii;
writePrecision  10;
runTimeModifiable true;
"""


def _fv_schemes() -> str:
    return _header(object_name="fvSchemes", class_name="dictionary", location="system") + """ddtSchemes
{
    default steadyState;
}
gradSchemes
{
    default Gauss linear;
}
divSchemes
{
    default none;
    div(phi,U) bounded Gauss linearUpwind grad(U);
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}
laplacianSchemes
{
    default Gauss linear corrected;
}
interpolationSchemes
{
    default linear;
}
snGradSchemes
{
    default corrected;
}
wallDist
{
    method meshWave;
}
"""


def _fv_solution(relative_tolerance: float) -> str:
    return _header(object_name="fvSolution", class_name="dictionary", location="system") + f"""solvers
{{
    p
    {{
        solver GAMG;
        tolerance {relative_tolerance:.17g};
        relTol 0.01;
        smoother GaussSeidel;
    }}
    U
    {{
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance {relative_tolerance:.17g};
        relTol 0.1;
    }}
}}

SIMPLE
{{
    nNonOrthogonalCorrectors 0;
    consistent yes;
    residualControl
    {{
        p {relative_tolerance:.17g};
        U {relative_tolerance:.17g};
    }}
}}

relaxationFactors
{{
    fields
    {{
        p 0.3;
    }}
    equations
    {{
        U 0.7;
    }}
}}
"""
