"""Bounded OpenFOAM lowering for a transient laminar baffled channel."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath

from .. import boundaries, engineering, initialization, outputs, procedures
from .._version import __version__
from .._validation import positive_float
from ..errors import ProviderUnavailableError, UnsupportedCaseError
from ..geometry import RectangularChannel
from .base import ProviderDescriptor
from ..results import Artifact, Check, FieldRecord, History, Quantity, SimulationResult
from .openfoam import (
    PreparedOpenFOAMCase,
    _analysis_sha256,
    _header,
    _latest_time_directory,
    _mesh_quality_quantities,
    _read_scalar_series,
    _runtime_version,
    _stop_timed_out_container,
    _write_mesh_manifest,
)


_CAPABILITY = "openfoam.transient-laminar-baffled-channel"
_RESERVED_REPORT_NAMES = {
    "agentcfd_inlet_flow",
    "agentcfd_outlet_flow",
    "agentcfd_inlet_pressure",
    "agentcfd_outlet_pressure",
    "agentcfd_vorticity",
}
_FOAM_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIELD_NAMES = {
    "fluid.velocity": "U",
    "fluid.pressure": "p",
    "fluid.vorticity": "vorticity",
}
_REPORT_OPERATIONS = {
    "minimum": "min",
    "maximum": "max",
    "area-average": "areaAverage",
    "area-integral": "areaIntegrate",
    "uniformity": "uniformity",
}


def _foam_name(value: str) -> str:
    """Lower a public display name to a deterministic OpenFOAM word."""

    selected = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())
    return selected if selected and (selected[0].isalpha() or selected[0] == "_") else f"r_{selected}"


def _positive_cells(length: float, size: float) -> int:
    return max(1, math.ceil(length / size - 1.0e-12))


def _foam_scalar(value: float) -> str:
    return f"{value:.17g}"


def _channel_block_mesh(domain: RectangularChannel, *, base_size: float) -> str:
    baffle = domain.baffles[0]
    xs = (0.0, baffle.x, baffle.x + baffle.thickness, domain.length)
    ys = (0.0, baffle.height, domain.height)
    zs = (0.0, domain.width)

    def vertex(i: int, j: int, k: int) -> int:
        return (i * len(ys) + j) * len(zs) + k

    vertex_lines = [
        f"    ({_foam_scalar(x)} {_foam_scalar(y)} {_foam_scalar(z)})"
        for x in xs
        for y in ys
        for z in zs
    ]
    nx = tuple(_positive_cells(right - left, base_size) for left, right in zip(xs, xs[1:]))
    ny = (
        _positive_cells(baffle.height, base_size),
        _positive_cells(domain.height - baffle.height, base_size),
    )
    nz = _positive_cells(domain.width, base_size)

    def block(name: str, i0: int, i1: int, j0: int, j1: int, cells: tuple[int, int, int]) -> str:
        labels = (
            vertex(i0, j0, 0),
            vertex(i1, j0, 0),
            vertex(i1, j1, 0),
            vertex(i0, j1, 0),
            vertex(i0, j0, 1),
            vertex(i1, j0, 1),
            vertex(i1, j1, 1),
            vertex(i0, j1, 1),
        )
        return (
            f"    name {name} hex ({' '.join(str(item) for item in labels)}) "
            f"({cells[0]} {cells[1]} {cells[2]}) simpleGrading (1 1 1)"
        )

    blocks = (
        block("upstreamLower", 0, 1, 0, 1, (nx[0], ny[0], nz)),
        block("upstreamUpper", 0, 1, 1, 2, (nx[0], ny[1], nz)),
        block("overBaffle", 1, 2, 1, 2, (nx[1], ny[1], nz)),
        block("downstreamLower", 2, 3, 0, 1, (nx[2], ny[0], nz)),
        block("downstreamUpper", 2, 3, 1, 2, (nx[2], ny[1], nz)),
    )
    return _header(
        object_name="blockMeshDict", class_name="dictionary", location="system"
    ) + f"""scale 1;

vertices
(
{chr(10).join(vertex_lines)}
);

blocks
(
{chr(10).join(blocks)}
);

edges ();

boundary
(
    inlet
    {{
        type patch;
        faces ((upstreamLower 0) (upstreamUpper 0));
    }}
    outlet
    {{
        type patch;
        faces ((downstreamLower 1) (downstreamUpper 1));
    }}
    walls
    {{
        type wall;
        faces
        (
            (upstreamLower 2) (downstreamLower 2)
            (upstreamUpper 3) (overBaffle 3) (downstreamUpper 3)
            (upstreamLower 4) (upstreamLower 5)
            (upstreamUpper 4) (upstreamUpper 5)
            (overBaffle 4) (overBaffle 5)
            (downstreamLower 4) (downstreamLower 5)
            (downstreamUpper 4) (downstreamUpper 5)
        );
    }}
    {baffle.name}
    {{
        type wall;
        faces ((upstreamLower 1) (overBaffle 2) (downstreamLower 0));
    }}
);

mergePatchPairs ();
"""


def _velocity_field(domain: RectangularChannel, velocity: float, initial: tuple[float, float, float]) -> str:
    wall_names = ("walls", domain.baffles[0].name)
    walls = "\n".join(
        f"    {name} {{ type noSlip; }}" for name in wall_names
    )
    return _header(object_name="U", class_name="volVectorField", location="0") + f"""dimensions [0 1 -1 0 0 0 0];
internalField uniform ({_foam_scalar(initial[0])} {_foam_scalar(initial[1])} {_foam_scalar(initial[2])});
boundaryField
{{
    inlet
    {{
        type fixedValue;
        value uniform ({_foam_scalar(velocity)} 0 0);
    }}
    outlet
    {{
        type pressureInletOutletVelocity;
        value uniform (0 0 0);
    }}
{walls}
}}
"""


def _pressure_field(domain: RectangularChannel, pressure: float) -> str:
    walls = "\n".join(
        f"    {name} {{ type zeroGradient; }}"
        for name in ("walls", domain.baffles[0].name)
    )
    return _header(object_name="p", class_name="volScalarField", location="0") + f"""dimensions [0 2 -2 0 0 0 0];
internalField uniform {_foam_scalar(pressure)};
boundaryField
{{
    inlet {{ type zeroGradient; }}
    outlet
    {{
        type fixedValue;
        value uniform {_foam_scalar(pressure)};
    }}
{walls}
}}
"""


def _transport_properties(viscosity: float) -> str:
    return _header(
        object_name="transportProperties", class_name="dictionary", location="constant"
    ) + f"""transportModel Newtonian;
nu [0 2 -1 0 0 0 0] {_foam_scalar(viscosity)};
"""


def _turbulence_properties() -> str:
    return _header(
        object_name="turbulenceProperties", class_name="dictionary", location="constant"
    ) + "simulationType laminar;\n"


def _fv_schemes() -> str:
    return _header(object_name="fvSchemes", class_name="dictionary", location="system") + """ddtSchemes
{
    default Euler;
}
gradSchemes
{
    default Gauss linear;
}
divSchemes
{
    default none;
    div(phi,U) Gauss linearUpwind grad(U);
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
fluxRequired
{
    default no;
    p;
    Phi;
}
"""


def _fv_solution(procedure: procedures.TransientProcedure, *, potential_iterations: int | None) -> str:
    phi_solver = "" if potential_iterations is None else f"""
    Phi
    {{
        solver PCG;
        preconditioner DIC;
        tolerance 1e-10;
        relTol 0;
        maxIter {potential_iterations};
    }}
"""
    return _header(object_name="fvSolution", class_name="dictionary", location="system") + f"""solvers
{{
    p
    {{
        solver GAMG;
        tolerance 1e-8;
        relTol 0.05;
        smoother GaussSeidel;
    }}
    pFinal
    {{
        $p;
        relTol 0;
    }}
    U
    {{
        solver smoothSolver;
        smoother symGaussSeidel;
        tolerance 1e-9;
        relTol 0.05;
    }}
    UFinal
    {{
        $U;
        relTol 0;
    }}
{phi_solver}}}

PIMPLE
{{
    momentumPredictor yes;
    nOuterCorrectors 1;
    nCorrectors {procedure.pressure_velocity_correctors};
    nNonOrthogonalCorrectors 0;
}}
"""


def _report_functions(step) -> str:
    definitions: list[str] = []
    if "fluid.vorticity" in step.output.fields:
        definitions.append("""
    agentcfd_vorticity
    {
        type vorticity;
        libs (fieldFunctionObjects);
        executeControl timeStep;
        executeInterval 1;
        writeControl outputTime;
    }
""")
    for report in step.output.reports:
        if isinstance(report, outputs.PointProbe):
            fields = " ".join(_FIELD_NAMES[name] for name in report.fields)
            point = " ".join(_foam_scalar(value) for value in report.location)
            definitions.append(f"""
    {_foam_name(report.name)}
    {{
        type probes;
        libs (sampling);
        probeLocations (({point}));
        fields ({fields});
        fixedLocations true;
        includeOutOfBounds false;
        interpolationScheme cellPoint;
        executeControl timeStep;
        executeInterval {report.every};
        writeControl timeStep;
        writeInterval {report.every};
    }}
""")
        elif isinstance(report, outputs.SurfaceReport):
            field = _FIELD_NAMES[report.field]
            operation = _REPORT_OPERATIONS[report.operation]
            definitions.append(f"""
    {_foam_name(report.name)}
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        regionType patch;
        name {report.region};
        operation {operation};
        fields ({field});
        writeFields false;
        executeControl timeStep;
        executeInterval {report.every};
        writeControl timeStep;
        writeInterval {report.every};
    }}
""")
        elif isinstance(report, outputs.ForceReport):
            patches = " ".join(report.regions)
            center = report.center or (0.0, 0.0, 0.0)
            center_value = " ".join(_foam_scalar(value) for value in center)
            definitions.append(f"""
    {_foam_name(report.name)}
    {{
        type forces;
        libs (forces);
        patches ({patches});
        p p;
        U U;
        rho rhoInf;
        rhoInf {_foam_scalar(step.model.fluid.density)};
        CofR ({center_value});
        executeControl timeStep;
        executeInterval {report.every};
        writeControl timeStep;
        writeInterval {report.every};
    }}
""")
    return "".join(definitions)


def _control_dict(step) -> str:
    procedure = step.procedure
    frames = step.output.frames
    field_interval = procedure.end_time if frames.mode == "final" else frames.every
    assert field_interval is not None
    return _header(object_name="controlDict", class_name="dictionary", location="system") + f"""application pimpleFoam;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime {_foam_scalar(procedure.end_time)};
deltaT {_foam_scalar(procedure.initial_time_step)};
adjustTimeStep yes;
maxCo {_foam_scalar(procedure.maximum_courant_number)};
maxDeltaT {_foam_scalar(procedure.maximum_time_step)};
writeControl adjustableRunTime;
writeInterval {_foam_scalar(field_interval)};
purgeWrite 0;
writeFormat binary;
writePrecision 10;
writeCompression off;
runTimeModifiable true;

functions
{{
    agentcfd_inlet_flow
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        regionType patch;
        name inlet;
        operation sum;
        fields (phi);
        writeFields false;
        writeControl timeStep;
        writeInterval 1;
    }}
    agentcfd_outlet_flow
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        regionType patch;
        name outlet;
        operation sum;
        fields (phi);
        writeFields false;
        writeControl timeStep;
        writeInterval 1;
    }}
    agentcfd_inlet_pressure
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        regionType patch;
        name inlet;
        operation areaAverage;
        fields (p);
        writeFields false;
        writeControl timeStep;
        writeInterval 1;
    }}
    agentcfd_outlet_pressure
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        regionType patch;
        name outlet;
        operation areaAverage;
        fields (p);
        writeFields false;
        writeControl timeStep;
        writeInterval 1;
    }}
{_report_functions(step)}}}
"""


class OpenFOAMChannelProvider:
    """First executable non-pipe slice: one wall-attached baffle."""

    def __init__(
        self,
        *,
        case_directory: str | Path | None = None,
        container_image: str | None = None,
        timeout_seconds: float = 3600.0,
    ) -> None:
        self.case_directory = Path(case_directory) if case_directory is not None else None
        self.container_image = str(container_image).strip() if container_image else None
        self.timeout_seconds = positive_float(timeout_seconds, name="timeout_seconds")

    def descriptor(self) -> ProviderDescriptor:
        available = shutil.which("docker") is not None if self.container_image else all(
            shutil.which(command) is not None
            for command in ("blockMesh", "checkMesh", "pimpleFoam")
        )
        return ProviderDescriptor(
            name="openfoam",
            version=self.container_image or os.environ.get("WM_PROJECT_VERSION", "externally-managed"),
            license="GPL-3.0-or-later (external program)",
            available=available,
            execution_boundary=(
                "filesystem-and-container-subprocess"
                if self.container_image
                else "filesystem-and-subprocess"
            ),
            capabilities=(_CAPABILITY,),
        )

    def validate(self, step) -> None:
        model = step.model
        if not isinstance(model.domain, RectangularChannel) or len(model.domain.baffles) != 1:
            raise UnsupportedCaseError(
                "The channel provider requires one rectangular channel with exactly one attached baffle."
            )
        if _FOAM_WORD.fullmatch(model.domain.baffles[0].name) is None:
            raise UnsupportedCaseError(
                "The baffle region name must be an OpenFOAM-compatible word in this provider."
            )
        if model.domain.baffles[0].attached_to != "bottom":
            raise UnsupportedCaseError(
                "The first channel provider lowers bottom-attached baffles only."
            )
        if (
            model.study.steady
            or model.study.compressible
            or model.study.energy
            or model.study.reacting
            or not model.study.laminar
        ):
            raise UnsupportedCaseError(
                "The channel provider supports transient incompressible isothermal laminar flow only."
            )
        if not isinstance(step.procedure, procedures.TransientProcedure):
            raise UnsupportedCaseError("The channel provider requires a transient procedure.")
        if step.mesh is None or step.mesh.method != "structured":
            raise UnsupportedCaseError(
                "The first channel lowering requires explicit meshing.structured intent."
            )
        if step.mesh.local_sizing or step.mesh.boundary_layers:
            raise UnsupportedCaseError(
                "Local sizing and boundary-layer lowering are not implemented for the first channel slice."
            )
        if model.domain.roughness != 0.0 or any(
            model.boundary_conditions[name].roughness not in {None, 0.0}
            for name in ("walls", model.domain.baffles[0].name)
            if isinstance(model.boundary_conditions.get(name), boundaries.NoSlipWall)
        ):
            raise UnsupportedCaseError(
                "The first channel slice supports hydraulically smooth walls only."
            )
        if step.initialization is not None and not isinstance(
            step.initialization,
            (initialization.UniformInitialization, initialization.PotentialFlowInitialization),
        ):
            raise UnsupportedCaseError(
                "The channel provider supports uniform or potential-flow initialization."
            )
        expected = {
            "inlet": boundaries.MeanVelocityInlet,
            "outlet": boundaries.PressureOutlet,
            "walls": boundaries.NoSlipWall,
            model.domain.baffles[0].name: boundaries.NoSlipWall,
        }
        if any(
            not isinstance(model.boundary_conditions.get(name), condition_type)
            for name, condition_type in expected.items()
        ):
            raise UnsupportedCaseError(
                "The first channel slice requires mean-velocity inlet, pressure outlet, "
                "and no-slip outer and baffle walls."
            )
        unsupported_fields = sorted(set(step.output.fields) - _FIELD_NAMES.keys())
        unsupported_histories = sorted(
            set(step.output.histories) - {"flow.mass_balance", "flow.pressure_drop"}
        )
        if unsupported_fields or unsupported_histories:
            raise UnsupportedCaseError(
                "The channel provider cannot recover requested outputs: "
                + ", ".join((*unsupported_fields, *unsupported_histories))
                + "."
            )
        lowered_report_names = [_foam_name(report.name) for report in step.output.reports]
        if (
            len(set(lowered_report_names)) != len(lowered_report_names)
            or set(lowered_report_names) & _RESERVED_REPORT_NAMES
        ):
            raise UnsupportedCaseError(
                "Report names collide with each other or a reserved provider monitor "
                "after deterministic OpenFOAM name lowering."
            )
        for report in step.output.reports:
            if isinstance(report, outputs.PointProbe):
                if set(report.fields) - {"fluid.velocity", "fluid.pressure"}:
                    raise UnsupportedCaseError(
                        f"Probe {report.name!r} supports velocity and pressure only."
                    )
            elif isinstance(report, outputs.SurfaceReport):
                if report.region not in model.domain.surface_names:
                    raise UnsupportedCaseError(
                        f"Surface report {report.name!r} references unknown region {report.region!r}."
                    )
                if report.field != "fluid.pressure":
                    raise UnsupportedCaseError(
                        f"Surface report {report.name!r} must currently reduce scalar pressure; "
                        "vector velocity needs an explicit component or magnitude contract."
                    )
                if report.operation not in _REPORT_OPERATIONS:
                    raise UnsupportedCaseError(
                        f"Surface report {report.name!r} requests an unsupported operation."
                    )
            elif not isinstance(report, outputs.ForceReport):
                raise UnsupportedCaseError("The channel provider received an unknown report type.")
            elif set(report.regions) - set(model.domain.surface_names):
                raise UnsupportedCaseError(
                    f"Force report {report.name!r} references an unknown surface region."
                )
        if step.output.checkpoints.enabled:
            raise UnsupportedCaseError(
                "Independent rolling checkpoint lowering is not yet implemented for the channel provider."
            )
        frames = step.output.frames
        if (
            frames.mode == "interval"
            and frames.include_final
            and not math.isclose(
                step.procedure.end_time / frames.every,
                round(step.procedure.end_time / frames.every),
                rel_tol=0.0,
                abs_tol=1e-10,
            )
        ):
            raise UnsupportedCaseError(
                "The current OpenFOAM channel writer requires the field-frame interval "
                "to divide end_time when include_final=True."
            )
        inlet = model.boundary_conditions["inlet"]
        reynolds = engineering.reynolds_number(
            density=model.fluid.density,
            mean_velocity=inlet.velocity,
            hydraulic_diameter=model.domain.hydraulic_diameter,
            dynamic_viscosity=model.fluid.dynamic_viscosity,
        )
        if reynolds >= 2300.0:
            raise UnsupportedCaseError(
                "The experimental laminar channel capability requires inlet hydraulic "
                f"Re < 2300; resolved Re={reynolds:.6g}. Declare supported turbulence "
                "physics instead of silently running a high-Re laminar model."
            )
        baffle = model.domain.baffles[0]
        for report in step.output.reports:
            if isinstance(report, outputs.PointProbe):
                x, y, z = report.location
                inside = (
                    0.0 <= x <= model.domain.length
                    and 0.0 <= y <= model.domain.height
                    and 0.0 <= z <= model.domain.width
                )
                in_solid = baffle.x <= x <= baffle.x + baffle.thickness and y <= baffle.height
                if not inside or in_solid:
                    raise UnsupportedCaseError(
                        f"Probe {report.name!r} must lie inside the fluid volume, not in the baffle."
                    )

    def prepare(self, step, directory: str | Path | None = None) -> PreparedOpenFOAMCase:
        step.model.validate()
        self.validate(step)
        target = Path(directory) if directory is not None else self.case_directory
        if target is None:
            raise ValueError("OpenFOAM case_directory is required for prepare or run.")
        if target.exists() and any(target.iterdir()):
            raise FileExistsError(f"OpenFOAM case directory is not empty: {target}")
        model = step.model
        inlet = model.boundary_conditions["inlet"]
        outlet = model.boundary_conditions["outlet"]
        initial_velocity = (
            step.initialization.velocity
            if isinstance(step.initialization, initialization.UniformInitialization)
            else (0.0, 0.0, 0.0)
        )
        initial_pressure_pa = (
            step.initialization.gauge_pressure
            if isinstance(step.initialization, initialization.UniformInitialization)
            else outlet.gauge_pressure
        )
        potential_iterations = (
            step.initialization.maximum_iterations
            if isinstance(step.initialization, initialization.PotentialFlowInitialization)
            else None
        )
        rendered = {
            "0/U": _velocity_field(model.domain, inlet.velocity, initial_velocity),
            "0/p": _pressure_field(
                model.domain,
                initial_pressure_pa / model.fluid.density,
            ),
            "constant/transportProperties": _transport_properties(
                model.fluid.kinematic_viscosity
            ),
            "constant/turbulenceProperties": _turbulence_properties(),
            "system/blockMeshDict": _channel_block_mesh(
                model.domain,
                base_size=step.mesh.base_size,
            ),
            "system/controlDict": _control_dict(step),
            "system/fvSchemes": _fv_schemes(),
            "system/fvSolution": _fv_solution(
                step.procedure,
                potential_iterations=potential_iterations,
            ),
        }
        target.mkdir(parents=True, exist_ok=True)
        hashes: dict[str, str] = {}
        for relative, content in sorted(rendered.items()):
            relative_path = PurePosixPath(relative)
            path = target.joinpath(*relative_path.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8")
            path.write_bytes(data)
            hashes[relative] = hashlib.sha256(data).hexdigest()
        case_identity = hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prepared = PreparedOpenFOAMCase(
            directory=target,
            model_sha256=model.fingerprint(),
            analysis_sha256=_analysis_sha256(step),
            case_sha256=case_identity,
            files=hashes,
            capability=_CAPABILITY,
        )
        (target / "agentcfd-case.json").write_text(
            json.dumps(prepared.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return prepared

    def _commands(self, step) -> dict[str, str | None]:
        names = ["blockMesh", "checkMesh"]
        if isinstance(step.initialization, initialization.PotentialFlowInitialization):
            names.append("potentialFoam")
        names.append("pimpleFoam")
        if self.container_image:
            docker = shutil.which("docker")
            return {name: docker for name in names}
        return {name: shutil.which(name) for name in names}

    def _argv(self, name: str, command: str, case: Path, cidfile: Path | None) -> list[str]:
        if not self.container_image:
            return [command, "-case", str(case)]
        argv = [command, "run", "--rm"]
        if cidfile is not None:
            argv.extend(("--cidfile", str(cidfile)))
        argv.extend(("-v", f"{case.resolve()}:/case", "-w", "/case", self.container_image, name, "-case", "/case"))
        return argv

    def run(self, step) -> SimulationResult:
        prepared = self.prepare(step)
        commands = self._commands(step)
        missing = [name for name, command in commands.items() if command is None]
        if missing:
            raise ProviderUnavailableError(
                "OpenFOAM channel execution requires: " + ", ".join(missing)
            )
        logs: dict[str, str] = {}
        return_codes: dict[str, int] = {}
        durations: dict[str, float] = {}
        for name, command in commands.items():
            assert command is not None
            cidfile = prepared.directory / f".agentcfd-{name}.cid" if self.container_image else None
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    self._argv(name, command, prepared.directory, cidfile),
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
                if cidfile is not None:
                    combined += _stop_timed_out_container(command, cidfile)
                return_codes[name] = -124
            finally:
                durations[name] = time.monotonic() - started
                if cidfile is not None:
                    cidfile.unlink(missing_ok=True)
            logs[name] = combined
            (prepared.directory / f"log.{name}").write_text(combined, encoding="utf-8")
            if return_codes[name] != 0:
                break
        return self._recover(step, prepared, logs, return_codes, durations)

    def _recover(self, step, prepared, logs, return_codes, durations) -> SimulationResult:
        process_ok = all(return_codes.get(name) == 0 for name in self._commands(step))
        solver_log = logs.get("pimpleFoam", "")
        reached_end = process_ok and bool(re.search(r"(?m)^End\s*$", solver_log))
        inlet_flow = _read_scalar_series(prepared.directory, "agentcfd_inlet_flow")
        outlet_flow = _read_scalar_series(prepared.directory, "agentcfd_outlet_flow")
        inlet_pressure = _read_scalar_series(prepared.directory, "agentcfd_inlet_pressure")
        outlet_pressure = _read_scalar_series(prepared.directory, "agentcfd_outlet_pressure")
        shared_times = sorted(set(inlet_flow) & set(outlet_flow))
        pressure_times = sorted(set(inlet_pressure) & set(outlet_pressure))
        imbalance_values = tuple(
            abs(inlet_flow[t] + outlet_flow[t]) / max(abs(inlet_flow[t]), abs(outlet_flow[t]), 1e-30)
            for t in shared_times
        )
        pressure_values = tuple(
            (inlet_pressure[t] - outlet_pressure[t]) * step.model.fluid.density
            for t in pressure_times
        )
        quantities: dict[str, Quantity] = {
            "flow.reynolds_number": Quantity(_inlet_reynolds(step), "1", kind="scientific_input"),
            **{
                f"runtime.{name}.wall_seconds": Quantity(value, "s", kind="runtime_metric")
                for name, value in durations.items()
            },
        }
        times_reached = [
            float(value)
            for value in re.findall(r"(?m)^Time = ([0-9.eE+-]+)\s*$", solver_log)
        ]
        courant_values = [
            float(value)
            for value in re.findall(
                r"Courant Number mean: [0-9.eE+-]+ max: ([0-9.eE+-]+)",
                solver_log,
            )
        ]
        final_time = times_reached[-1] if times_reached else None
        maximum_courant = max(courant_values) if courant_values else None
        if final_time is not None:
            quantities["solver.final_time"] = Quantity(final_time, "s", kind="runtime_metric")
        if maximum_courant is not None:
            quantities["solver.maximum_courant_number"] = Quantity(
                maximum_courant, "1", kind="verification_metric"
            )
        histories: dict[str, History] = {}
        if imbalance_values:
            quantities["flow.relative_mass_imbalance"] = Quantity(imbalance_values[-1], "1")
            histories["flow.relative_mass_imbalance"] = History(tuple(shared_times), imbalance_values, unit="1")
        if pressure_values:
            quantities["flow.pressure_drop"] = Quantity(pressure_values[-1], "Pa")
            histories["flow.pressure_drop"] = History(tuple(pressure_times), pressure_values, unit="Pa")
        quantities.update(_mesh_quality_quantities(logs.get("checkMesh", "")))
        quality = step.mesh.quality
        mesh_gate_specs = (
            ("mesh-maximum-aspect-ratio", "mesh.maximum_aspect_ratio", quality.maximum_aspect_ratio),
            ("mesh-maximum-non-orthogonality", "mesh.maximum_non_orthogonality", quality.maximum_non_orthogonality),
            ("mesh-maximum-skewness", "mesh.maximum_skewness", quality.maximum_skewness),
        )
        mesh_gate_checks = tuple(
            Check(
                name=check_name,
                passed=metric_name in quantities and quantities[metric_name].value <= limit,
                value=quantities[metric_name].value if metric_name in quantities else None,
                limit=limit,
                observable=metric_name,
            )
            for check_name, metric_name, limit in mesh_gate_specs
        )
        mesh_sha, mesh_manifest = _write_mesh_manifest(prepared.directory)
        artifacts = {
            "case_manifest": Artifact.from_path(prepared.directory / "agentcfd-case.json", role="input-manifest", media_type="application/json"),
            **{
                f"log_{name}": Artifact.from_path(prepared.directory / f"log.{name}", role="execution-log", media_type="text/plain")
                for name in logs
            },
        }
        if mesh_manifest is not None:
            artifacts["mesh_manifest"] = Artifact.from_path(mesh_manifest, role="mesh-manifest", media_type="application/json")
        fields: dict[str, FieldRecord] = {}
        latest = _latest_time_directory(prepared.directory)
        if latest is not None:
            for canonical, native, unit, components in (
                ("fluid.velocity", "U", "m/s", ("x", "y", "z")),
                ("fluid.pressure", "p", "m^2/s^2", ()),
                ("fluid.vorticity", "vorticity", "1/s", ("x", "y", "z")),
            ):
                path = latest / native
                if canonical in step.output.fields and path.is_file():
                    fields[native] = FieldRecord(unit=unit, location="cell", artifact=str(path), components=components, mesh_sha256=mesh_sha)
                    artifacts[f"field_{native}"] = Artifact.from_path(path, role="native-field")
        self._recover_reports(step, prepared.directory, quantities, histories, artifacts)
        mass_ok = bool(imbalance_values) and imbalance_values[-1] <= 1e-4
        time_ok = final_time is not None and math.isclose(
            final_time,
            step.procedure.end_time,
            rel_tol=0.0,
            abs_tol=max(1e-12, step.procedure.end_time * 1e-10),
        )
        courant_limit = step.procedure.maximum_courant_number * 1.05
        courant_ok = maximum_courant is not None and maximum_courant <= courant_limit
        requested_history_map = {
            "flow.mass_balance": "flow.relative_mass_imbalance",
            "flow.pressure_drop": "flow.pressure_drop",
        }
        missing = [name for name in step.output.histories if requested_history_map[name] not in histories]
        missing += [name for name in step.output.fields if _FIELD_NAMES[name] not in fields]
        missing += [
            f"report:{report.name}"
            for report in step.output.reports
            if not _report_recovered(report, histories)
        ]
        runtime_version = _runtime_version(logs, self.descriptor().version)
        checks = (
            Check("openfoam-process", process_ok, value=json.dumps(return_codes, sort_keys=True), limit="all return codes equal zero", kind="runtime"),
            Check("solver-completion-marker", reached_end, value="found" if reached_end else "missing", limit="pimpleFoam log ends with End", kind="runtime"),
            Check("transient-time-horizon", time_ok, value=final_time, limit=step.procedure.end_time, observable="solver.time"),
            Check("courant-control", courant_ok, value=maximum_courant, limit=courant_limit, observable="solver.maximum_courant_number"),
            Check("mesh-quality", return_codes.get("checkMesh") == 0 and "Mesh OK" in logs.get("checkMesh", ""), value="Mesh OK" if "Mesh OK" in logs.get("checkMesh", "") else "not confirmed", limit="checkMesh succeeds and reports Mesh OK"),
            *mesh_gate_checks,
            Check("transient-mass-balance", mass_ok, value=imbalance_values[-1] if imbalance_values else None, limit=1e-4, observable="flow.mass_balance"),
            Check("mesh-identity", mesh_sha is not None, value=mesh_sha or "missing", limit="content-addressed polyMesh exists"),
            Check("openfoam-runtime-version", runtime_version.startswith("v2606") or runtime_version == "2606", value=runtime_version, limit="OpenCFD v2606", kind="runtime"),
            Check("requested-output-completeness", not missing, value="complete" if not missing else ", ".join(missing), limit="all requested fields and histories recovered", kind="runtime"),
        )
        return SimulationResult(
            status="completed" if process_ok else "failed",
            converged=reached_end and time_ok and courant_ok and mass_ok,
            provider="openfoam",
            quantities=quantities,
            histories=histories,
            fields=fields,
            artifacts=artifacts,
            checks=checks,
            scientific_inputs=step.to_dict(),
            provenance={
                "agentcfd_version": __version__,
                "model_sha256": prepared.model_sha256,
                "analysis_sha256": prepared.analysis_sha256,
                "case_sha256": prepared.case_sha256,
                "provider_capability": _CAPABILITY,
                "runtime_version": runtime_version,
            },
            messages=("Experimental low-Re transient baffled-channel capability; no physical validation claim is made.",),
            name=step.model.name,
        )

    def _recover_reports(self, step, case, quantities, histories, artifacts) -> None:
        density = step.model.fluid.density
        for report in step.output.reports:
            function_name = _foam_name(report.name)
            root = case / "postProcessing" / function_name
            if isinstance(report, outputs.SurfaceReport):
                samples = _read_scalar_series(case, function_name)
                factor = density if report.field == "fluid.pressure" else 1.0
                values = tuple(value * factor for _, value in sorted(samples.items()))
                times = tuple(time_value for time_value, _ in sorted(samples.items()))
                if values:
                    name = f"report.{report.name}"
                    histories[name] = History(times, values, unit="Pa")
                    quantities[name] = Quantity(values[-1], "Pa")
            elif isinstance(report, outputs.PointProbe):
                for field_name in report.fields:
                    native = _FIELD_NAMES[field_name]
                    rows = _read_numeric_rows(root / "0" / native)
                    if not rows:
                        continue
                    factor = density if field_name == "fluid.pressure" else 1.0
                    components = ("x", "y", "z") if field_name == "fluid.velocity" else ("value",)
                    unit = "m/s" if field_name == "fluid.velocity" else "Pa"
                    for index, component in enumerate(components, start=1):
                        selected = [(row[0], row[index] * factor) for row in rows if len(row) > index]
                        if not selected:
                            continue
                        name = f"probe.{report.name}.{field_name}.{component}"
                        histories[name] = History(
                            tuple(item[0] for item in selected),
                            tuple(item[1] for item in selected),
                            unit=unit,
                        )
                        quantities[name] = Quantity(selected[-1][1], unit)
            elif isinstance(report, outputs.ForceReport):
                rows = _read_numeric_rows(root / "0" / "force.dat")
                selected = [
                    (row[0], sum(row[i + 1] * report.direction[i] for i in range(3)))
                    for row in rows
                    if len(row) >= 4
                ]
                if selected:
                    name = f"report.{report.name}"
                    histories[name] = History(
                        tuple(item[0] for item in selected),
                        tuple(item[1] for item in selected),
                        unit="N",
                    )
                    quantities[name] = Quantity(selected[-1][1], "N")
            if root.is_dir():
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        artifacts[f"report_{function_name}_{path.name}"] = Artifact.from_path(
                            path, role="compact-report", media_type="text/plain"
                        )


def _inlet_reynolds(step) -> float:
    inlet = step.model.boundary_conditions["inlet"]
    return engineering.reynolds_number(
        density=step.model.fluid.density,
        mean_velocity=inlet.velocity,
        hydraulic_diameter=step.model.domain.hydraulic_diameter,
        dynamic_viscosity=step.model.fluid.dynamic_viscosity,
    )


def _read_numeric_rows(path: Path) -> list[tuple[float, ...]]:
    if not path.is_file():
        return []
    rows: list[tuple[float, ...]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            values = tuple(float(value) for value in stripped.replace("(", " ").replace(")", " ").split())
        except ValueError:
            continue
        if values and all(math.isfinite(value) for value in values):
            rows.append(values)
    return rows


def _report_recovered(report, histories: dict[str, History]) -> bool:
    if isinstance(report, outputs.PointProbe):
        return all(
            any(name.startswith(f"probe.{report.name}.{field}.") for name in histories)
            for field in report.fields
        )
    return f"report.{report.name}" in histories


__all__ = ["OpenFOAMChannelProvider"]
