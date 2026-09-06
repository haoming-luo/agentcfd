"""Shared compact OpenFOAM report lowering and recovery."""

from __future__ import annotations

import math
import re
from pathlib import Path

from .. import outputs
from ..results import Artifact, History, Quantity


FIELD_NAMES = {
    "fluid.velocity": "U",
    "fluid.pressure": "p",
    "fluid.vorticity": "vorticity",
}
REPORT_OPERATIONS = {
    "minimum": "min",
    "maximum": "max",
    "area-average": "areaAverage",
    "area-integral": "areaIntegrate",
    "uniformity": "uniformity",
}
RESERVED_REPORT_NAMES = {
    "agentcfd_inlet_flow",
    "agentcfd_outlet_flow",
    "agentcfd_inlet_pressure",
    "agentcfd_outlet_pressure",
    "agentcfd_vorticity",
}


def foam_name(value: str) -> str:
    """Lower a display name to a deterministic OpenFOAM word."""

    selected = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())
    return (
        selected
        if selected and (selected[0].isalpha() or selected[0] == "_")
        else f"r_{selected}"
    )


def _foam_scalar(value: float) -> str:
    return f"{value:.17g}"


def render_report_functions(step, *, include_vorticity: bool = False) -> str:
    """Render validated compact reports without retaining volume fields."""

    definitions: list[str] = []
    if include_vorticity and "fluid.vorticity" in step.output.fields:
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
            fields = " ".join(FIELD_NAMES[name] for name in report.fields)
            point = " ".join(_foam_scalar(value) for value in report.location)
            definitions.append(f"""
    {foam_name(report.name)}
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
            definitions.append(f"""
    {foam_name(report.name)}
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        regionType patch;
        name {report.region};
        operation {REPORT_OPERATIONS[report.operation]};
        fields ({FIELD_NAMES[report.field]});
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
    {foam_name(report.name)}
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


def _read_numeric_rows(path: Path) -> list[tuple[float, ...]]:
    if not path.is_file():
        return []
    rows: list[tuple[float, ...]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            values = tuple(
                float(value)
                for value in stripped.replace("(", " ").replace(")", " ").split()
            )
        except ValueError:
            continue
        if values and all(math.isfinite(value) for value in values):
            rows.append(values)
    return rows


def read_segmented_rows(root: Path, filename: str) -> list[tuple[float, ...]]:
    """Merge function-object files across restart start-time directories."""

    by_time: dict[float, tuple[float, ...]] = {}
    if not root.is_dir():
        return []
    candidates: list[tuple[float, Path]] = []
    for path in root.glob(f"*/{filename}"):
        try:
            segment_start = float(path.parent.name)
        except ValueError:
            continue
        candidates.append((segment_start, path))
    for _, path in sorted(candidates):
        for row in _read_numeric_rows(path):
            by_time[row[0]] = row
    return [by_time[time_value] for time_value in sorted(by_time)]


def recover_reports(
    step,
    case: Path,
    quantities: dict[str, Quantity],
    histories: dict[str, History],
    artifacts: dict[str, Artifact],
) -> None:
    """Recover compact reports in SI with canonical names."""

    density = step.model.fluid.density
    coordinate = {
        "abscissa_name": (
            "solver_iteration" if step.model.study.steady else "time"
        ),
        "abscissa_unit": "1" if step.model.study.steady else "s",
    }
    for report in step.output.reports:
        function_name = foam_name(report.name)
        root = case / "postProcessing" / function_name
        if isinstance(report, outputs.SurfaceReport):
            rows = read_segmented_rows(root, "surfaceFieldValue.dat")
            selected = [(row[0], row[1]) for row in rows if len(row) >= 2]
            if report.field == "fluid.pressure":
                factor = 1.0 if report.operation == "uniformity" else density
                unit = (
                    "1"
                    if report.operation == "uniformity"
                    else "N"
                    if report.operation == "area-integral"
                    else "Pa"
                )
            else:
                factor = 1.0
                unit = "m/s"
            if selected:
                name = f"report.{report.name}"
                values = tuple(value * factor for _, value in selected)
                histories[name] = History(
                    tuple(time_value for time_value, _ in selected),
                    values,
                    unit=unit,
                    **coordinate,
                )
                quantities[name] = Quantity(values[-1], unit)
        elif isinstance(report, outputs.PointProbe):
            for field_name in report.fields:
                rows = read_segmented_rows(root, FIELD_NAMES[field_name])
                if not rows:
                    continue
                factor = density if field_name == "fluid.pressure" else 1.0
                components = (
                    ("x", "y", "z")
                    if field_name == "fluid.velocity"
                    else ("value",)
                )
                unit = "m/s" if field_name == "fluid.velocity" else "Pa"
                for index, component in enumerate(components, start=1):
                    selected = [
                        (row[0], row[index] * factor)
                        for row in rows
                        if len(row) > index
                    ]
                    if not selected:
                        continue
                    name = f"probe.{report.name}.{field_name}.{component}"
                    histories[name] = History(
                        tuple(item[0] for item in selected),
                        tuple(item[1] for item in selected),
                        unit=unit,
                        **coordinate,
                    )
                    quantities[name] = Quantity(selected[-1][1], unit)
        elif isinstance(report, outputs.ForceReport):
            rows = read_segmented_rows(root, "force.dat")
            selected = []
            for row in rows:
                force_vectors = [
                    row[offset : offset + 3]
                    for offset in (1, 4, 7)
                    if len(row) >= offset + 3
                ]
                if not force_vectors:
                    continue
                total = tuple(
                    sum(vector[index] for vector in force_vectors)
                    for index in range(3)
                )
                selected.append(
                    (
                        row[0],
                        sum(
                            total[index] * report.direction[index]
                            for index in range(3)
                        ),
                    )
                )
            if selected:
                name = f"report.{report.name}"
                histories[name] = History(
                    tuple(item[0] for item in selected),
                    tuple(item[1] for item in selected),
                    unit="N",
                    **coordinate,
                )
                quantities[name] = Quantity(selected[-1][1], "N")
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    artifact_name = foam_name(
                        f"report_{function_name}_{relative.replace('/', '_')}"
                    )
                    artifacts[artifact_name] = Artifact.from_path(
                        path,
                        role="compact-report",
                        media_type="text/plain",
                    )


def report_recovered(report, histories: dict[str, History]) -> bool:
    if isinstance(report, outputs.PointProbe):
        return all(
            any(name.startswith(f"probe.{report.name}.{field}.") for name in histories)
            for field in report.fields
        )
    return f"report.{report.name}" in histories


__all__ = [
    "FIELD_NAMES",
    "REPORT_OPERATIONS",
    "RESERVED_REPORT_NAMES",
    "foam_name",
    "read_segmented_rows",
    "recover_reports",
    "render_report_functions",
    "report_recovered",
]
