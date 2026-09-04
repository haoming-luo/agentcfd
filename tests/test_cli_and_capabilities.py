import json

import jsonschema
import pytest

from agentcfd import Check, Quantity, SimulationResult, benchmarks, capabilities, contracts, licensing, properties
from agentcfd.cli import _result_cli_payload, build_parser, entrypoint, main
from agentcfd.providers import OpenFOAMProvider


def test_capability_catalog_is_truthful():
    maturity = {item.name: item.maturity for item in capabilities.all()}
    assert maturity["reference.hagen-poiseuille"] == "release"
    assert maturity["provider.openfoam"] == "experimental"
    assert maturity["openfoam.steady-laminar-circular-pipe"] == "experimental"
    assert maturity["engineering.gas-screening"] == "experimental"
    assert maturity["validation.single-observable-uncertainty"] == "experimental"
    report = capabilities.as_dict()
    jsonschema.Draft202012Validator(
        contracts.load("capability-catalog.schema.json")
    ).validate(report)


def test_cli_result_payload_exposes_failed_decision_gates_to_agents():
    result = SimulationResult(
        status="completed",
        converged=True,
        provider="test",
        quantities={},
        checks=(
            Check("runtime", True, kind="runtime"),
            Check(
                "reference-applicability",
                False,
                kind="validation",
                observable="boundary.inlet_profile",
            ),
        ),
    )

    decision = _result_cli_payload(result)["decision"]
    assert decision["accepted"] is False
    assert decision["failed_check_count"] == 1
    assert decision["failed_checks"][0]["name"] == "reference-applicability"
    assert "do not promote" in decision["guidance"]


def test_cli_demo_writes_accepted_result(tmp_path, capsys):
    target = tmp_path / "pipe.json"
    assert main(["demo", "pipe", "--output", str(target)]) == 0
    payload = json.loads(target.read_text())
    assert payload["accepted"] is True
    assert "Accepted laminar pipe result" in capsys.readouterr().out


def test_cli_doctor_json(capsys):
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["healthy"] is True
    assert payload["providers"]["reference-pipe"] is True
    assert "checkMesh" in payload["executables"]


def test_benchmark_catalog_is_machine_readable_and_cli_visible(capsys):
    report = benchmarks.as_dict()
    ids = {case["id"] for case in report["cases"]}
    assert "laminar-fully-developed-pipe" in ids
    assert "fda-benchmark-nozzle" in ids
    assert "single-phase-if97-steam-pipe" in ids
    assert "iaea-tee-junction-thermal-mixing" in ids
    assert "sandia-tnf-nonpremixed-flame" in ids
    assert "nist-multiphase-spray-flame" in ids
    assert all(case["source_url"].startswith("https://") for case in report["cases"])
    assert all(
        case["redistribution_status"] == "link-only-pending-terms-review"
        for case in report["cases"]
    )

    assert main(["benchmarks", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report


def test_installed_contract_catalog_is_loadable_and_cli_visible(capsys):
    assert "simulation-result.schema.json" in contracts.available()
    result_schema = contracts.load("simulation-result.schema.json")
    assert result_schema["$schema"].endswith("2020-12/schema")
    assert contracts.path("simulation-result.schema.json").is_file()

    assert main(["contracts", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "agentcfd.contract-catalog/0.1"
    assert len(payload["contracts"]) == len(contracts.available())
    with pytest.raises(KeyError, match="Unknown AgentCFD contract"):
        contracts.load("../untrusted.json")


def test_license_catalog_keeps_copyleft_solver_external(capsys):
    report = licensing.as_dict()
    components = {item["name"]: item for item in report["components"]}
    assert report["core_has_mandatory_third_party_runtime_dependencies"] is False
    assert components["agentcfd"]["license_expression"] == "Apache-2.0"
    assert components["OpenFOAM"]["relationship"] == "user-managed-external-process"
    assert components["OpenFOAM"]["mandatory_runtime"] is False

    assert main(["licenses", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report


def test_cli_calculates_forward_and_inverse_pipe_operating_point(capsys):
    common = [
        "--density",
        "1000",
        "--viscosity",
        "0.001",
        "--length",
        "2",
        "--diameter",
        "0.1",
        "--json",
    ]
    assert main(["calculate", "pipe-loss", *common, "--velocity", "0.01"]) == 0
    loss = json.loads(capsys.readouterr().out)
    assert loss["total_pressure_loss"] == pytest.approx(0.064)

    assert (
        main(
            [
                "calculate",
                "pipe-flow",
                *common,
                "--pressure-loss",
                "0.064",
                "--regime",
                "laminar",
            ]
        )
        == 0
    )
    flow = json.loads(capsys.readouterr().out)
    assert flow["mean_velocity"] == pytest.approx(0.01)


def test_cli_exposes_auditable_compressibility_screen(capsys):
    assert (
        main(
            [
                "calculate",
                "compressibility",
                "--velocity",
                "100",
                "--speed-of-sound",
                "400",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "incompressible_model_appropriate": True,
        "mach_number": 0.25,
        "maximum_incompressible_mach": 0.3,
    }


def test_cli_exposes_versioned_thermophysical_state(monkeypatch, capsys):
    state = properties.ThermophysicalState(
        fluid="IF97::Water",
        backend="IF97",
        pressure=101325.0,
        temperature=500.0,
        phase="gas",
        density=0.4409,
        dynamic_viscosity=1.73e-5,
        specific_heat=1981.5,
        thermal_conductivity=0.036,
        speed_of_sound=548.0,
        prandtl_number=0.952,
        provider="coolprop-properties",
        provider_version="test",
    )
    monkeypatch.setattr(
        properties.CoolPropPropertyProvider,
        "at_pressure_temperature",
        lambda self, fluid, *, pressure, temperature: state,
    )
    assert (
        main(
            [
                "properties",
                "state",
                "--fluid",
                "IF97::Water",
                "--pressure",
                "101325",
                "--temperature",
                "500",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["backend"] == "IF97"
    assert report["provider_version"] == "test"
    assert report["density"] == pytest.approx(0.4409)


def test_cli_prepares_openfoam_case_without_runtime(tmp_path, capsys):
    case_directory = tmp_path / "foam-case"
    assert main(["prepare", "openfoam-pipe", str(case_directory), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["capability"] == "openfoam.steady-laminar-circular-pipe"
    assert (case_directory / "agentcfd-case.json").is_file()
    assert (case_directory / "system" / "blockMeshDict").is_file()


def test_cli_prepares_turbulent_openfoam_pipe_with_explicit_inputs(tmp_path, capsys):
    case_directory = tmp_path / "turbulent-foam-case"
    assert (
        main(
            [
                "prepare",
                "openfoam-turbulent-pipe",
                str(case_directory),
                "--velocity",
                "1.2",
                "--turbulence-intensity",
                "0.04",
                "--turbulence-length-scale",
                "0.006",
                "--cross-section-cells",
                "4",
                "--axial-cells",
                "20",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["capability"] == "openfoam.steady-rans-smooth-circular-pipe"
    assert payload["schema"] == "agentcfd.openfoam-case/0.3"
    assert "flowRateInletVelocity" in (case_directory / "0" / "U").read_text()
    assert "kOmegaSST" in (
        case_directory / "constant" / "turbulenceProperties"
    ).read_text()


def test_cli_prepares_declared_fully_developed_openfoam_case(tmp_path, capsys):
    case_directory = tmp_path / "foam-case"
    assert (
        main(
            [
                "prepare",
                "openfoam-pipe",
                str(case_directory),
                "--fully-developed",
                "--cross-section-cells",
                "4",
                "--axial-cells",
                "20",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    velocity = (case_directory / "0" / "U").read_text()
    assert "type uniformFixedValue;" in velocity
    assert "type expression;" in velocity
    mesh = (case_directory / "system" / "blockMeshDict").read_text()
    assert mesh.count("(4 4 20) simpleGrading") == 5


def test_cli_prepares_same_model_openfoam_grid_family(tmp_path, capsys):
    root = tmp_path / "grid-study"
    assert (
        main(
            [
                "prepare",
                "openfoam-pipe-grid",
                str(root),
                "--cross-section-cells",
                "4",
                "8",
                "16",
                "--base-axial-cells",
                "20",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "agentcfd.openfoam-grid-study/0.1"
    assert payload["scientific_inputs"]["model"]["domain"]["length"] == 0.5
    assert payload["scientific_inputs"]["model"]["domain"]["diameter"] == 0.1
    assert [case["expected_cell_count"] for case in payload["cases"]] == [
        1600,
        12800,
        102400,
    ]


def test_cli_exposes_hash_verified_prepared_case_execution():
    args = build_parser().parse_args(
        [
            "run",
            "openfoam-pipe",
            "case",
            "--prepared",
            "--fully-developed",
            "--cross-section-cells",
            "16",
            "--axial-cells",
            "80",
            "--timeout-seconds",
            "120",
        ]
    )
    assert args.prepared is True
    assert args.cross_section_cells == 16
    assert args.axial_cells == 80
    assert args.timeout_seconds == 120.0


def test_cli_executes_prepared_grid_family_and_writes_gci(tmp_path, capsys, monkeypatch):
    root = tmp_path / "grid-study"
    assert main(["prepare", "openfoam-pipe-grid", str(root), "--json"]) == 0
    capsys.readouterr()
    values = {8: 1.0256, 16: 1.0064, 32: 1.0016}

    def fake_run_prepared(self, step, directory=None):
        cross = self.mesh.cross_section_cells
        axial = self.mesh.axial_cells
        return SimulationResult(
            status="completed",
            converged=True,
            provider="openfoam",
            quantities={
                "mesh.cell_count": Quantity(5 * cross**2 * axial, "1"),
                "flow.pressure_drop": Quantity(values[cross], "Pa"),
            },
            checks=(Check("synthetic-runtime", True, kind="runtime"),),
            provenance={"model_sha256": step.model.fingerprint()},
        )

    monkeypatch.setattr(OpenFOAMProvider, "run_prepared", fake_run_prepared)
    assert main(["run", "openfoam-pipe-grid", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["observed_order"] == pytest.approx(2.0)
    assert payload["extrapolated_value"] == pytest.approx(1.0)
    assert payload["acceptance"]["accepted"] is True
    jsonschema.Draft202012Validator(
        contracts.load("grid-convergence.schema.json")
    ).validate(payload)
    assert (root / "agentcfd-grid-convergence.json").is_file()
    assert all((root / case / "agentcfd-result.json").is_file() for case in (
        "grid-1-c8-a40",
        "grid-2-c16-a80",
        "grid-3-c32-a160",
    ))


def test_cli_computes_gci_from_three_result_files(tmp_path, capsys):
    paths = []
    for index, (cells, value) in enumerate(
        ((64, 1.0256), (512, 1.0064), (4096, 1.0016))
    ):
        path = tmp_path / f"result-{index}.json"
        SimulationResult(
            status="completed",
            converged=True,
            provider="synthetic",
            quantities={
                "mesh.cell_count": Quantity(cells, "1"),
                "flow.pressure_drop": Quantity(value, "Pa"),
            },
            checks=(Check("synthetic-convergence", True, kind="verification"),),
            provenance={"model_sha256": "a" * 64},
        ).write(path)
        paths.append(path)

    assert (
        main(
            [
                "verify",
                "grid-convergence",
                *(str(path) for path in paths),
                "--quantity",
                "flow.pressure_drop",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["observed_order"] == pytest.approx(2.0)
    assert payload["acceptance"]["accepted"] is True
    assert payload["schema"] == "agentcfd.grid-convergence/0.1"
    assert all(len(item["sha256"]) == 64 for item in payload["sources"])

    assert main(["verify", "result", str(paths[0]), "--json"]) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["verified"] is True
    assert verification["trust_level"] == "verified"


def test_cli_validation_point_uses_completed_unaccepted_status(capsys):
    common = [
        "verify",
        "validation-point",
        "--reference",
        "100",
        "--numerical-uncertainty",
        "0.3",
        "--input-uncertainty",
        "0.4",
        "--experimental-uncertainty",
        "0.5",
        "--json",
    ]
    assert main([*common, "--simulation", "101"]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["schema"] == "agentcfd.validation-point/0.1"
    assert accepted["accepted"] is True

    assert main([*common, "--simulation", "102"]) == 3
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["accepted"] is False


def test_cli_rejects_grid_study_above_uncertainty_limit(tmp_path, capsys):
    paths = []
    for index, (cells, value) in enumerate(((64, 3.56), (512, 1.64), (4096, 1.16))):
        path = tmp_path / f"result-{index}.json"
        SimulationResult(
            status="completed",
            converged=True,
            provider="synthetic",
            quantities={
                "mesh.cell_count": Quantity(cells, "1"),
                "flow.pressure_drop": Quantity(value, "Pa"),
            },
            checks=(Check("synthetic-convergence", True, kind="verification"),),
            provenance={"model_sha256": "a" * 64},
        ).write(path)
        paths.append(path)

    assert (
        main(
            [
                "verify",
                "grid-convergence",
                *(str(path) for path in paths),
                "--quantity",
                "flow.pressure_drop",
                "--json",
            ]
        )
        == 3
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["acceptance"]["accepted"] is False
    assert payload["acceptance"]["checks"][0]["name"] == "fine-grid-relative-gci"


def test_cli_openfoam_run_is_nonzero_when_scientific_checks_fail(
    tmp_path, capsys, monkeypatch
):
    result = SimulationResult(
        status="completed",
        converged=True,
        provider="openfoam",
        quantities={"flow.pressure_drop": Quantity(1.0, "Pa")},
        checks=(Check("pressure-validation", False, kind="validation"),),
    )
    target = tmp_path / "result.json"
    monkeypatch.setattr(
        "agentcfd.cli._run_openfoam_pipe",
        lambda *args, **kwargs: (result, target),
    )

    assert main(["run", "openfoam-pipe", str(tmp_path / "case")]) == 3
    assert "accepted false" in capsys.readouterr().out


def test_console_entrypoint_reports_expected_errors_without_traceback(tmp_path, capsys):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("user data")

    assert entrypoint(["prepare", "openfoam-pipe", str(occupied)]) == 2
    captured = capsys.readouterr()
    assert "agentcfd: error:" in captured.err
    assert "Traceback" not in captured.err


def test_grid_study_cli_rejects_ambiguous_json_plan(tmp_path, capsys):
    root = tmp_path / "grid-study"
    root.mkdir()
    (root / "agentcfd-grid-study.json").write_text(
        '{"schema":"agentcfd.openfoam-grid-study/0.1",'
        '"schema":"agentcfd.openfoam-grid-study/0.1"}'
    )

    assert entrypoint(["run", "openfoam-pipe-grid", str(root)]) == 2
    captured = capsys.readouterr()
    assert "duplicate key" in captured.err
    assert "Traceback" not in captured.err


def test_grid_study_cli_rejects_changed_procedure_inputs(tmp_path, capsys):
    root = tmp_path / "grid-study"
    assert main(["prepare", "openfoam-pipe-grid", str(root), "--json"]) == 0
    capsys.readouterr()
    plan_path = root / "agentcfd-grid-study.json"
    plan = json.loads(plan_path.read_text())
    plan["scientific_inputs"]["procedure"]["relative_tolerance"] = 1.0e-6
    plan_path.write_text(json.dumps(plan))

    assert entrypoint(["run", "openfoam-pipe-grid", str(root)]) == 2
    assert "different model, procedure, or output" in capsys.readouterr().err
