"""Small, stable CLI for people, agents, CI, and future GUIs."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import benchmarks, boundaries, capabilities, fluids, geometry, outputs, procedures, properties, studies
from ._version import __version__
from .model import Model
from .provenance import file_sha256
from .providers import OpenFOAMMeshControls, OpenFOAMProvider, prepare_pipe_grid_study
from .verification import grid_convergence_from_result_records


def _doctor() -> dict[str, object]:
    openfoam = OpenFOAMProvider().descriptor()
    coolprop = properties.CoolPropPropertyProvider().descriptor()
    try:
        numpy_version: str | None = version("numpy")
    except PackageNotFoundError:
        numpy_version = None
    return {
        "schema": "agentcfd.doctor/0.1",
        "healthy": True,
        "agentcfd": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": numpy_version,
        "executables": {
            "blockMesh": shutil.which("blockMesh"),
            "checkMesh": shutil.which("checkMesh"),
            "simpleFoam": shutil.which("simpleFoam"),
        },
        "providers": {
            "reference-pipe": True,
            "openfoam-runtime": openfoam.available,
            "coolprop-properties": coolprop.available,
        },
    }


def _pipe_model(*, fully_developed: bool = False) -> Model:
    inlet = (
        boundaries.fully_developed_velocity_inlet(0.02)
        if fully_developed
        else boundaries.mean_velocity_inlet(0.02)
    )
    return Model(
        name="laminar-water-pipe",
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=10.0, diameter=0.05),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=inlet,
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def _pipe_grid_benchmark_model() -> Model:
    """Return the bounded, aspect-ratio-conscious three-grid pipe benchmark."""

    return Model(
        name="laminar-water-pipe-grid-benchmark",
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=0.5, diameter=0.1),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.fully_developed_velocity_inlet(0.01),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def _pipe_demo(output_path: Path) -> dict[str, object]:
    result = _pipe_model().step(procedure=procedures.steady(), output=outputs.standard()).run()
    result.write(output_path)
    return result.to_dict()


def _prepare_openfoam_pipe(
    case_directory: Path,
    *,
    fully_developed: bool = False,
    cross_section_cells: int = 8,
    axial_cells: int | None = None,
) -> dict[str, object]:
    step = _pipe_model(fully_developed=fully_developed).step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
    mesh = OpenFOAMMeshControls(
        cross_section_cells=cross_section_cells,
        axial_cells=axial_cells,
    )
    return OpenFOAMProvider(case_directory=case_directory, mesh=mesh).prepare(step).to_dict()


def _run_openfoam_pipe(
    case_directory: Path,
    *,
    result_path: Path | None,
    fully_developed: bool,
    container_image: str | None,
    cross_section_cells: int,
    axial_cells: int | None,
    prepared: bool,
):
    step = _pipe_model(fully_developed=fully_developed).step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
    provider = OpenFOAMProvider(
        case_directory=case_directory,
        container_image=container_image,
        mesh=OpenFOAMMeshControls(
            cross_section_cells=cross_section_cells,
            axial_cells=axial_cells,
        ),
    )
    result = provider.run_prepared(step) if prepared else provider.run(step)
    target = result_path or case_directory / "agentcfd-result.json"
    result.write(target)
    return result, target


def _prepare_openfoam_pipe_grid(
    directory: Path,
    *,
    cross_section_cells: tuple[int, int, int],
    base_axial_cells: int,
) -> dict[str, object]:
    step = _pipe_grid_benchmark_model().step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
    return prepare_pipe_grid_study(
        step,
        directory,
        cross_section_cells=cross_section_cells,
        base_axial_cells=base_axial_cells,
    ).to_dict()


def _grid_convergence_payload(
    paths: list[Path],
    *,
    quantity: str,
) -> dict[str, object]:
    records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    study = grid_convergence_from_result_records(records, quantity=quantity)
    return {
        "schema": "agentcfd.grid-convergence/0.1",
        "quantity": quantity,
        "sources": [{"path": str(path), "sha256": file_sha256(path)} for path in paths],
        **study.to_dict(),
    }


def _run_openfoam_pipe_grid(
    directory: Path,
    *,
    container_image: str | None,
) -> tuple[dict[str, object], Path]:
    plan_path = directory / "agentcfd-grid-study.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != "agentcfd.openfoam-grid-study/0.1":
        raise ValueError("OpenFOAM grid-study plan schema is unsupported.")
    step = _pipe_grid_benchmark_model().step(
        procedure=procedures.steady(),
        output=outputs.standard(),
    )
    if plan.get("model_sha256") != step.model.fingerprint():
        raise ValueError("OpenFOAM grid-study plan belongs to a different benchmark model.")
    cases = plan.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        raise ValueError("OpenFOAM grid-study plan must contain exactly three cases.")

    root = directory.resolve()
    result_paths: list[Path] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("OpenFOAM grid-study case record is invalid.")
        case_directory = (directory / str(case.get("directory", ""))).resolve()
        try:
            case_directory.relative_to(root)
        except ValueError as error:
            raise ValueError("OpenFOAM grid-study case escapes the study directory.") from error
        manifest = json.loads(
            (case_directory / "agentcfd-case.json").read_text(encoding="utf-8")
        )
        if manifest.get("case_sha256") != case.get("case_sha256"):
            raise ValueError("OpenFOAM grid-study case identity differs from its plan.")
        mesh = OpenFOAMMeshControls(
            cross_section_cells=case.get("cross_section_cells"),
            axial_cells=case.get("axial_cells"),
        )
        result = OpenFOAMProvider(
            case_directory=case_directory,
            mesh=mesh,
            container_image=container_image,
        ).run_prepared(step)
        result_path = case_directory / "agentcfd-result.json"
        result.write(result_path)
        result_paths.append(result_path)

    payload = _grid_convergence_payload(result_paths, quantity="flow.pressure_drop")
    target = directory / "agentcfd-grid-convergence.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return payload, target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentcfd", description="AI-native CFD for humans and agents.")
    parser.add_argument("--version", action="version", version=f"AgentCFD {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Inspect the installed runtime.")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    catalog = subparsers.add_parser("capabilities", help="Show truthful capability boundaries.")
    catalog.add_argument("--json", action="store_true", dest="as_json")

    benchmark_catalog = subparsers.add_parser(
        "benchmarks",
        help="Show the evidence-gated benchmark roadmap.",
    )
    benchmark_catalog.add_argument("--json", action="store_true", dest="as_json")

    demo = subparsers.add_parser("demo", help="Run a bundled verified workflow.")
    demo_subparsers = demo.add_subparsers(dest="demo", required=True)
    pipe = demo_subparsers.add_parser("pipe", help="Run the laminar circular-pipe reference workflow.")
    pipe.add_argument("--output", type=Path, default=Path("agentcfd-pipe-result.json"))

    prepare = subparsers.add_parser("prepare", help="Generate a provider case without executing it.")
    prepare_subparsers = prepare.add_subparsers(dest="provider", required=True)
    openfoam = prepare_subparsers.add_parser(
        "openfoam-pipe",
        help="Generate the experimental OpenFOAM laminar-pipe case.",
    )
    openfoam.add_argument("case_directory", type=Path)
    openfoam.add_argument("--json", action="store_true", dest="as_json")
    openfoam.add_argument(
        "--fully-developed",
        action="store_true",
        help="Use a declared analytic laminar inlet profile.",
    )
    openfoam.add_argument(
        "--cross-section-cells",
        type=int,
        default=8,
        help="Cells along each O-grid block direction (default: 8).",
    )
    openfoam.add_argument(
        "--axial-cells",
        type=int,
        help="Cells along the pipe; defaults to a bounded geometry-based value.",
    )
    grid_prepare = prepare_subparsers.add_parser(
        "openfoam-pipe-grid",
        help="Prepare a same-model three-grid fully developed pipe study.",
    )
    grid_prepare.add_argument("directory", type=Path)
    grid_prepare.add_argument(
        "--cross-section-cells",
        nargs=3,
        type=int,
        default=(4, 8, 16),
        metavar=("COARSE", "MEDIUM", "FINE"),
    )
    grid_prepare.add_argument("--base-axial-cells", type=int, default=20)
    grid_prepare.add_argument("--json", action="store_true", dest="as_json")

    run = subparsers.add_parser("run", help="Prepare, execute, and recover a provider result.")
    run_subparsers = run.add_subparsers(dest="provider", required=True)
    run_openfoam = run_subparsers.add_parser(
        "openfoam-pipe",
        help="Run and recover the experimental OpenFOAM laminar-pipe case.",
    )
    run_openfoam.add_argument("case_directory", type=Path)
    run_openfoam.add_argument("--result", type=Path)
    run_openfoam.add_argument(
        "--container-image",
        help="Run OpenFOAM through Docker, for example opencfd/openfoam-run:2606.",
    )
    run_openfoam.add_argument("--json", action="store_true", dest="as_json")
    run_openfoam.add_argument(
        "--prepared",
        action="store_true",
        help="Verify hashes and execute an existing AgentCFD-prepared case.",
    )
    run_openfoam.add_argument(
        "--fully-developed",
        action="store_true",
        help="Use a declared analytic laminar inlet profile.",
    )
    run_openfoam.add_argument(
        "--cross-section-cells",
        type=int,
        default=8,
        help="Cells along each O-grid block direction (default: 8).",
    )
    run_openfoam.add_argument(
        "--axial-cells",
        type=int,
        help="Cells along the pipe; defaults to a bounded geometry-based value.",
    )
    run_grid = run_subparsers.add_parser(
        "openfoam-pipe-grid",
        help="Execute a prepared three-grid pipe study and compute GCI.",
    )
    run_grid.add_argument("directory", type=Path)
    run_grid.add_argument(
        "--container-image",
        help="Run all three cases through the selected Docker image.",
    )
    run_grid.add_argument("--json", action="store_true", dest="as_json")

    verify = subparsers.add_parser("verify", help="Create numerical verification evidence.")
    verify_subparsers = verify.add_subparsers(dest="verification", required=True)
    grid = verify_subparsers.add_parser(
        "grid-convergence",
        help="Compute a three-result Richardson extrapolation and GCI.",
    )
    grid.add_argument("results", nargs=3, type=Path)
    grid.add_argument("--quantity", required=True)
    grid.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        report = _doctor()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"AgentCFD {report['agentcfd']} | Python {report['python']} | healthy")
            print(f"Reference provider: ready | OpenFOAM runtime: {'found' if report['providers']['openfoam-runtime'] else 'not found (optional)'}")
        return 0
    if args.command == "capabilities":
        report = capabilities.as_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for item in capabilities.all():
                print(f"{item.name}: {item.maturity}")
        return 0
    if args.command == "benchmarks":
        report = benchmarks.as_dict()
        if args.as_json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for case in benchmarks.all():
                print(f"{case.id}: {case.status} | next: {case.next_gate}")
        return 0
    if args.command == "demo" and args.demo == "pipe":
        result = _pipe_demo(args.output)
        pressure_drop = result["quantities"]["flow.pressure_drop"]
        print(f"Accepted laminar pipe result | pressure drop {pressure_drop['value']:.6g} {pressure_drop['unit']}")
        print(args.output)
        return 0
    if args.command == "prepare" and args.provider == "openfoam-pipe":
        manifest = _prepare_openfoam_pipe(
            args.case_directory,
            fully_developed=args.fully_developed,
            cross_section_cells=args.cross_section_cells,
            axial_cells=args.axial_cells,
        )
        if args.as_json:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        else:
            print("Prepared experimental OpenFOAM circular-pipe case")
            print(args.case_directory)
            print(f"case sha256: {manifest['case_sha256']}")
        return 0
    if args.command == "prepare" and args.provider == "openfoam-pipe-grid":
        plan = _prepare_openfoam_pipe_grid(
            args.directory,
            cross_section_cells=tuple(args.cross_section_cells),
            base_axial_cells=args.base_axial_cells,
        )
        if args.as_json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print("Prepared same-model three-grid OpenFOAM pipe study")
            print(args.directory / "agentcfd-grid-study.json")
        return 0
    if args.command == "run" and args.provider == "openfoam-pipe":
        result, target = _run_openfoam_pipe(
            args.case_directory,
            result_path=args.result,
            fully_developed=args.fully_developed,
            container_image=args.container_image,
            cross_section_cells=args.cross_section_cells,
            axial_cells=args.axial_cells,
            prepared=args.prepared,
        )
        if args.as_json:
            print(json.dumps(result.summary(), indent=2, sort_keys=True))
        else:
            print(
                f"OpenFOAM run {result.status} | trust {result.trust_level} | "
                f"accepted {str(result.accepted).lower()}"
            )
            print(target)
        return 0 if result.status == "completed" else 1
    if args.command == "run" and args.provider == "openfoam-pipe-grid":
        payload, target = _run_openfoam_pipe_grid(
            args.directory,
            container_image=args.container_image,
        )
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                f"Completed three-grid study | observed order "
                f"{payload['observed_order']:.6g}"
            )
            print(target)
        return 0
    if args.command == "verify" and args.verification == "grid-convergence":
        payload = _grid_convergence_payload(args.results, quantity=args.quantity)
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            relative = payload["fine_grid_relative_gci"]
            relative_text = "undefined" if relative is None else f"{relative:.6g}"
            print(
                f"GCI {args.quantity} | observed order {payload['observed_order']:.6g} | "
                f"fine relative GCI {relative_text}"
            )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
