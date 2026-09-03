"""Small, stable CLI for people, agents, CI, and future GUIs."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from pathlib import Path

import numpy as np

from . import boundaries, capabilities, fluids, geometry, outputs, procedures, studies
from ._version import __version__
from .model import Model
from .providers import OpenFOAMProvider


def _doctor() -> dict[str, object]:
    openfoam = OpenFOAMProvider().descriptor()
    return {
        "schema": "agentcfd.doctor/0.1",
        "healthy": True,
        "agentcfd": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": np.__version__,
        "executables": {
            "blockMesh": shutil.which("blockMesh"),
            "simpleFoam": shutil.which("simpleFoam"),
        },
        "providers": {
            "reference-pipe": True,
            "openfoam-runtime": openfoam.available,
        },
    }


def _pipe_model() -> Model:
    return Model(
        name="laminar-water-pipe",
        study=studies.internal_flow(),
        domain=geometry.circular_pipe(length=10.0, diameter=0.05),
        fluid=fluids.newtonian("water", density=998.2, dynamic_viscosity=1.002e-3),
    ).boundaries(
        inlet=boundaries.mean_velocity_inlet(0.02),
        outlet=boundaries.pressure_outlet(),
        wall=boundaries.no_slip_wall(),
    )


def _pipe_demo(output_path: Path) -> dict[str, object]:
    result = _pipe_model().step(procedure=procedures.steady(), output=outputs.standard()).run()
    result.write(output_path)
    return result.to_dict()


def _prepare_openfoam_pipe(case_directory: Path) -> dict[str, object]:
    step = _pipe_model().step(procedure=procedures.steady(), output=outputs.standard())
    return OpenFOAMProvider(case_directory=case_directory).prepare(step).to_dict()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentcfd", description="AI-native CFD for humans and agents.")
    parser.add_argument("--version", action="version", version=f"AgentCFD {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Inspect the installed runtime.")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    catalog = subparsers.add_parser("capabilities", help="Show truthful capability boundaries.")
    catalog.add_argument("--json", action="store_true", dest="as_json")

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
    if args.command == "demo" and args.demo == "pipe":
        result = _pipe_demo(args.output)
        pressure_drop = result["quantities"]["flow.pressure_drop"]
        print(f"Accepted laminar pipe result | pressure drop {pressure_drop['value']:.6g} {pressure_drop['unit']}")
        print(args.output)
        return 0
    if args.command == "prepare" and args.provider == "openfoam-pipe":
        manifest = _prepare_openfoam_pipe(args.case_directory)
        if args.as_json:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        else:
            print("Prepared experimental OpenFOAM circular-pipe case")
            print(args.case_directory)
            print(f"case sha256: {manifest['case_sha256']}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
