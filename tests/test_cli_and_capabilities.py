import json

import pytest

from agentcfd import benchmarks, capabilities
from agentcfd.cli import main


def test_capability_catalog_is_truthful():
    maturity = {item.name: item.maturity for item in capabilities.all()}
    assert maturity["reference.hagen-poiseuille"] == "release"
    assert maturity["provider.openfoam"] == "experimental"
    assert maturity["openfoam.steady-laminar-circular-pipe"] == "experimental"


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
    assert all(case["source_url"].startswith("https://") for case in report["cases"])

    assert main(["benchmarks", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == report


def test_cli_prepares_openfoam_case_without_runtime(tmp_path, capsys):
    case_directory = tmp_path / "foam-case"
    assert main(["prepare", "openfoam-pipe", str(case_directory), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["capability"] == "openfoam.steady-laminar-circular-pipe"
    assert (case_directory / "agentcfd-case.json").is_file()
    assert (case_directory / "system" / "blockMeshDict").is_file()


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


def test_cli_computes_gci_from_three_result_files(tmp_path, capsys):
    paths = []
    for index, (cells, value) in enumerate(((64, 3.56), (512, 1.64), (4096, 1.16))):
        path = tmp_path / f"result-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "converged": True,
                    "provenance": {"model_sha256": "a" * 64},
                    "quantities": {
                        "mesh.cell_count": {"value": cells, "unit": "1"},
                        "flow.pressure_drop": {"value": value, "unit": "Pa"},
                    },
                }
            )
        )
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
    assert payload["schema"] == "agentcfd.grid-convergence/0.1"
    assert all(len(item["sha256"]) == 64 for item in payload["sources"])
