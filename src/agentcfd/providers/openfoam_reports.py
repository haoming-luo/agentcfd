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
    "agentcfd_total_pressure",
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


def report_function_names(report: outputs.Report) -> tuple[str, ...]:
    """Return every OpenFOAM function-object name owned by a public report."""

    if isinstance(report, outputs.PressureLossReport):
        stem = foam_name(report.name)
        return (
            f"agentcfd_loss_{stem}_inlet",
            f"agentcfd_loss_{stem}_outlet",
        )
    if isinstance(report, outputs.FlowUniformityReport):
        stem = foam_name(report.name)
        return (
            f"agentcfd_uniformity_{stem}",
            f"agentcfd_uniformity_{stem}_normal",
        )
    if isinstance(report, outputs.FlowDistributionReport):
        stem = foam_name(report.name)
        return tuple(
            f"agentcfd_distribution_{stem}_{foam_name(region)}"
            for region in (report.inlet, *report.outlets)
        )
    return (foam_name(report.name),)


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
    pressure_loss_reports = tuple(
        report
        for report in step.output.reports
        if isinstance(report, outputs.PressureLossReport)
    )
    if pressure_loss_reports:
        definitions.append(f"""
    agentcfd_total_pressure
    {{
        type pressure;
        libs (fieldFunctionObjects);
        mode total;
        p p;
        U U;
        rho rhoInf;
        rhoInf {_foam_scalar(step.model.fluid.density)};
        pRef 0;
        result agentcfd_total_pressure;
        executeControl timeStep;
        executeInterval 1;
        writeControl none;
    }}
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
        elif isinstance(report, outputs.PressureLossReport):
            inlet_function, outlet_function = report_function_names(report)
            for function_name, region in (
                (inlet_function, report.inlet),
                (outlet_function, report.outlet),
            ):
                definitions.append(f"""
    {function_name}
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        regionType patch;
        name {region};
        operation weightedAverage;
        weightField phi;
        fields (agentcfd_total_pressure);
        writeArea true;
        writeFields false;
        executeControl timeStep;
        executeInterval {report.every};
        writeControl timeStep;
        writeInterval {report.every};
    }}
""")
        elif isinstance(report, outputs.FlowUniformityReport):
            uniformity_function, normal_function = report_function_names(report)
            for function_name, operation in (
                (uniformity_function, "uniformity"),
                (normal_function, "areaNormalAverage"),
            ):
                definitions.append(f"""
    {function_name}
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        regionType patch;
        name {report.region};
        operation {operation};
        fields (U);
        writeArea true;
        writeFields false;
        executeControl timeStep;
        executeInterval {report.every};
        writeControl timeStep;
        writeInterval {report.every};
    }}
""")
        elif isinstance(report, outputs.FlowDistributionReport):
            for function_name, region in zip(
                report_function_names(report),
                (report.inlet, *report.outlets),
            ):
                definitions.append(f"""
    {function_name}
    {{
        type surfaceFieldValue;
        libs (fieldFunctionObjects);
        regionType patch;
        name {region};
        operation sum;
        fields (phi);
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


def read_surface_area(
    root: Path, filename: str = "surfaceFieldValue.dat"
) -> float | None:
    """Recover the constant patch area recorded in a surface report header."""

    if not root.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for path in root.glob(f"*/{filename}"):
        try:
            segment_start = float(path.parent.name)
        except ValueError:
            continue
        candidates.append((segment_start, path))
    for _, path in sorted(candidates, reverse=True):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(
                r"^\s*#\s*Area\s*:\s*([0-9.eE+-]+)\s*$",
                line,
                flags=re.IGNORECASE,
            )
            if match is None:
                continue
            try:
                area = float(match.group(1))
            except ValueError:
                continue
            if math.isfinite(area) and area > 0.0:
                return area
    return None


def _store_scalar_history(
    name: str,
    samples: list[tuple[float, float]],
    *,
    unit: str,
    quantities: dict[str, Quantity],
    histories: dict[str, History],
    coordinate: dict[str, str],
    description: str,
) -> None:
    if not samples:
        return
    values = tuple(value for _, value in samples)
    histories[name] = History(
        tuple(time_value for time_value, _ in samples),
        values,
        unit=unit,
        description=description,
        **coordinate,
    )
    quantities[name] = Quantity(values[-1], unit, description=description)


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
        artifact_roots = (root,)
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
        elif isinstance(report, outputs.PressureLossReport):
            inlet_function, outlet_function = report_function_names(report)
            inlet_root = case / "postProcessing" / inlet_function
            outlet_root = case / "postProcessing" / outlet_function
            artifact_roots = (inlet_root, outlet_root)
            inlet_rows = read_segmented_rows(inlet_root, "surfaceFieldValue.dat")
            outlet_rows = read_segmented_rows(outlet_root, "surfaceFieldValue.dat")
            flow_rows = read_segmented_rows(
                case / "postProcessing" / "agentcfd_inlet_flow",
                "surfaceFieldValue.dat",
            )
            # ``writeArea true`` inserts Area between Time and the reduced field.
            # The final column also supports runtimes that keep area in the header.
            inlet_values = {row[0]: row[-1] for row in inlet_rows if len(row) >= 2}
            outlet_values = {
                row[0]: row[-1] for row in outlet_rows if len(row) >= 2
            }
            flow_values = {row[0]: row[1] for row in flow_rows if len(row) >= 2}
            shared = sorted(set(inlet_values) & set(outlet_values) & set(flow_values))
            area = report.reference_area or read_surface_area(inlet_root)
            prefix = f"report.{report.name}"
            if shared and area is not None:
                inlet_total = [(time, inlet_values[time]) for time in shared]
                outlet_total = [(time, outlet_values[time]) for time in shared]
                losses = [
                    (time, inlet_values[time] - outlet_values[time])
                    for time in shared
                ]
                velocities = [
                    (time, abs(flow_values[time]) / area) for time in shared
                ]
                dynamic_pressures = [
                    (time, 0.5 * density * velocity**2)
                    for time, velocity in velocities
                ]
                coefficients = [
                    (time, loss / dynamic_pressure)
                    for (time, loss), (_, dynamic_pressure) in zip(
                        losses, dynamic_pressures
                    )
                    if dynamic_pressure > 0.0
                ]
                for suffix, samples, unit, description in (
                    (
                        "inlet_total_pressure",
                        inlet_total,
                        "Pa",
                        "Mass-flow-averaged physical total pressure at the inlet.",
                    ),
                    (
                        "outlet_total_pressure",
                        outlet_total,
                        "Pa",
                        "Mass-flow-averaged physical total pressure at the outlet.",
                    ),
                    (
                        "total_pressure_loss",
                        losses,
                        "Pa",
                        "Inlet minus outlet mass-flow-averaged total pressure.",
                    ),
                    (
                        "reference_bulk_velocity",
                        velocities,
                        "m/s",
                        "Actual inlet volume flow divided by the reference area.",
                    ),
                    (
                        "reference_dynamic_pressure",
                        dynamic_pressures,
                        "Pa",
                        "Dynamic pressure from fluid density and inlet bulk velocity.",
                    ),
                    (
                        "loss_coefficient",
                        coefficients,
                        "1",
                        "Total-pressure loss divided by inlet reference dynamic pressure.",
                    ),
                ):
                    _store_scalar_history(
                        f"{prefix}.{suffix}",
                        samples,
                        unit=unit,
                        quantities=quantities,
                        histories=histories,
                        coordinate=coordinate,
                        description=description,
                    )
                quantities[f"{prefix}.reference_area"] = Quantity(
                    area,
                    "m^2",
                    kind="scientific_input" if report.reference_area else "diagnostic",
                    description=(
                        "Explicit loss-coefficient reference area."
                        if report.reference_area
                        else "Inlet patch area recovered from the OpenFOAM report."
                    ),
                )
        elif isinstance(report, outputs.FlowUniformityReport):
            uniformity_function, normal_function = report_function_names(report)
            uniformity_root = case / "postProcessing" / uniformity_function
            normal_root = case / "postProcessing" / normal_function
            artifact_roots = (uniformity_root, normal_root)
            uniformity_rows = read_segmented_rows(
                uniformity_root, "surfaceFieldValue.dat"
            )
            normal_rows = read_segmented_rows(normal_root, "surfaceFieldValue.dat")

            # Vector reductions are emitted as ``(x y z)`` while scalar
            # reductions use one value. ``writeArea true`` inserts area after
            # time; the first vector component owns these scalar semantics.
            def reduced_value(row: tuple[float, ...]) -> float | None:
                if len(row) >= 5:
                    return row[-3]
                if len(row) >= 3:
                    return row[-1]
                return None

            uniformity_values = {
                row[0]: value
                for row in uniformity_rows
                if (value := reduced_value(row)) is not None
                and -1.0e-12 <= value <= 1.0 + 1.0e-12
            }
            normal_values = {
                row[0]: value
                for row in normal_rows
                if (value := reduced_value(row)) is not None
            }
            shared = sorted(set(uniformity_values) & set(normal_values))
            uniformity_area = read_surface_area(uniformity_root)
            normal_area = read_surface_area(normal_root)
            areas_match = (
                uniformity_area is not None
                and normal_area is not None
                and math.isclose(
                    uniformity_area,
                    normal_area,
                    rel_tol=1.0e-9,
                    abs_tol=1.0e-15,
                )
            )
            if shared and areas_match:
                prefix = f"report.{report.name}"
                uniformity = [
                    (time, min(1.0, max(0.0, uniformity_values[time])))
                    for time in shared
                ]
                normal_velocity = [(time, normal_values[time]) for time in shared]
                _store_scalar_history(
                    f"{prefix}.velocity_uniformity_index",
                    uniformity,
                    unit="1",
                    quantities=quantities,
                    histories=histories,
                    coordinate=coordinate,
                    description=(
                        "Area-weighted velocity-vector uniformity index; one is "
                        "perfectly uniform and zero is maximally nonuniform."
                    ),
                )
                _store_scalar_history(
                    f"{prefix}.area_normal_velocity",
                    normal_velocity,
                    unit="m/s",
                    quantities=quantities,
                    histories=histories,
                    coordinate=coordinate,
                    description=(
                        "Area-average velocity normal to the surface; positive follows "
                        "the surface outward normal."
                    ),
                )
                quantities[f"{prefix}.area"] = Quantity(
                    uniformity_area,
                    "m^2",
                    kind="diagnostic",
                    description="Surface area used by the flow-uniformity reductions.",
                )
        elif isinstance(report, outputs.FlowDistributionReport):
            regions = (report.inlet, *report.outlets)
            function_names = report_function_names(report)
            artifact_roots = tuple(
                case / "postProcessing" / function_name
                for function_name in function_names
            )
            series = {
                region: {
                    row[0]: row[-1]
                    for row in read_segmented_rows(root, "surfaceFieldValue.dat")
                    if len(row) >= 2
                }
                for region, root in zip(regions, artifact_roots)
            }
            shared = sorted(set.intersection(*(set(values) for values in series.values())))
            role_flows = {
                report.inlet: {
                    time: -series[report.inlet][time] for time in shared
                },
                **{
                    outlet: {time: series[outlet][time] for time in shared}
                    for outlet in report.outlets
                },
            }
            valid_times = [
                time
                for time in shared
                if role_flows[report.inlet][time] > 0.0
                and all(role_flows[outlet][time] >= 0.0 for outlet in report.outlets)
                and sum(role_flows[outlet][time] for outlet in report.outlets) > 0.0
            ]
            # Do not silently publish an earlier valid split when the final state
            # contains inlet/outlet reversal.
            if shared and shared[-1] in valid_times:
                prefix = f"report.{report.name}"
                inlet_volume = [
                    (time, role_flows[report.inlet][time]) for time in valid_times
                ]
                total_outlet = [
                    (
                        time,
                        sum(role_flows[outlet][time] for outlet in report.outlets),
                    )
                    for time in valid_times
                ]
                relative_imbalance = [
                    (
                        time,
                        abs(inlet_value - outlet_value)
                        / max(abs(inlet_value), abs(outlet_value), 1.0e-300),
                    )
                    for (time, inlet_value), (_, outlet_value) in zip(
                        inlet_volume, total_outlet
                    )
                ]
                coefficient_of_variation: list[tuple[float, float]] = []
                for time in valid_times:
                    values = [role_flows[outlet][time] for outlet in report.outlets]
                    mean = sum(values) / len(values)
                    coefficient_of_variation.append(
                        (
                            time,
                            math.sqrt(
                                sum((value - mean) ** 2 for value in values)
                                / len(values)
                            )
                            / mean,
                        )
                    )
                for suffix, samples, unit, description in (
                    (
                        "inlet.volume_flow_rate",
                        inlet_volume,
                        "m^3/s",
                        "Inlet flow into the domain, positive in the declared role direction.",
                    ),
                    (
                        "inlet.mass_flow_rate",
                        [(time, value * density) for time, value in inlet_volume],
                        "kg/s",
                        "Constant-density inlet mass flow into the domain.",
                    ),
                    (
                        "total_outlet_volume_flow_rate",
                        total_outlet,
                        "m^3/s",
                        "Sum of declared outlet volume-flow rates.",
                    ),
                    (
                        "total_outlet_mass_flow_rate",
                        [(time, value * density) for time, value in total_outlet],
                        "kg/s",
                        "Constant-density sum of declared outlet mass-flow rates.",
                    ),
                    (
                        "relative_imbalance",
                        relative_imbalance,
                        "1",
                        "Absolute inlet-versus-total-outlet flow imbalance.",
                    ),
                    (
                        "coefficient_of_variation",
                        coefficient_of_variation,
                        "1",
                        "Population standard deviation of outlet flows divided by their mean.",
                    ),
                ):
                    _store_scalar_history(
                        f"{prefix}.{suffix}",
                        samples,
                        unit=unit,
                        quantities=quantities,
                        histories=histories,
                        coordinate=coordinate,
                        description=description,
                    )

                target_by_outlet = dict(report.target_fractions)
                maximum_errors: list[tuple[float, float]] = []
                for outlet in report.outlets:
                    outlet_volume = [
                        (time, role_flows[outlet][time]) for time in valid_times
                    ]
                    fractions = [
                        (time, value / total)
                        for (time, value), (_, total) in zip(
                            outlet_volume, total_outlet
                        )
                    ]
                    for suffix, samples, unit, description in (
                        (
                            "volume_flow_rate",
                            outlet_volume,
                            "m^3/s",
                            f"Flow through outlet {outlet!r}, positive out of the domain.",
                        ),
                        (
                            "mass_flow_rate",
                            [(time, value * density) for time, value in outlet_volume],
                            "kg/s",
                            f"Constant-density mass flow through outlet {outlet!r}.",
                        ),
                        (
                            "fraction",
                            fractions,
                            "1",
                            f"Outlet {outlet!r} share of total declared outlet flow.",
                        ),
                    ):
                        _store_scalar_history(
                            f"{prefix}.outlet.{outlet}.{suffix}",
                            samples,
                            unit=unit,
                            quantities=quantities,
                            histories=histories,
                            coordinate=coordinate,
                            description=description,
                        )
                    if outlet in target_by_outlet:
                        target = target_by_outlet[outlet]
                        quantities[f"{prefix}.outlet.{outlet}.target_fraction"] = Quantity(
                            target,
                            "1",
                            kind="scientific_input",
                            description=f"Declared target flow fraction for outlet {outlet!r}.",
                        )
                        errors = [
                            (time, fraction - target) for time, fraction in fractions
                        ]
                        _store_scalar_history(
                            f"{prefix}.outlet.{outlet}.fraction_error",
                            errors,
                            unit="1",
                            quantities=quantities,
                            histories=histories,
                            coordinate=coordinate,
                            description=(
                                f"Actual minus target flow fraction for outlet {outlet!r}."
                            ),
                        )
                if target_by_outlet:
                    for index, time in enumerate(valid_times):
                        maximum_errors.append(
                            (
                                time,
                                max(
                                    abs(
                                        role_flows[outlet][time]
                                        / total_outlet[index][1]
                                        - target_by_outlet[outlet]
                                    )
                                    for outlet in report.outlets
                                ),
                            )
                        )
                    _store_scalar_history(
                        f"{prefix}.maximum_fraction_error",
                        maximum_errors,
                        unit="1",
                        quantities=quantities,
                        histories=histories,
                        coordinate=coordinate,
                        description="Largest absolute outlet target-fraction error.",
                    )
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
        for artifact_root in artifact_roots:
            if not artifact_root.is_dir():
                continue
            for path in sorted(artifact_root.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(artifact_root).as_posix()
                    artifact_name = foam_name(
                        f"report_{artifact_root.name}_{relative.replace('/', '_')}"
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
    if isinstance(report, outputs.PressureLossReport):
        return f"report.{report.name}.loss_coefficient" in histories
    if isinstance(report, outputs.FlowUniformityReport):
        prefix = f"report.{report.name}"
        return all(
            f"{prefix}.{suffix}" in histories
            for suffix in ("velocity_uniformity_index", "area_normal_velocity")
        )
    if isinstance(report, outputs.FlowDistributionReport):
        prefix = f"report.{report.name}"
        required = {
            f"{prefix}.inlet.volume_flow_rate",
            f"{prefix}.relative_imbalance",
            *(f"{prefix}.outlet.{outlet}.fraction" for outlet in report.outlets),
        }
        if report.target_fractions:
            required.add(f"{prefix}.maximum_fraction_error")
        return required <= histories.keys()
    return f"report.{report.name}" in histories


__all__ = [
    "FIELD_NAMES",
    "REPORT_OPERATIONS",
    "RESERVED_REPORT_NAMES",
    "foam_name",
    "read_segmented_rows",
    "read_surface_area",
    "recover_reports",
    "render_report_functions",
    "report_recovered",
    "report_function_names",
]
