import json

from agentcfd import capabilities
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
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    velocity = (case_directory / "0" / "U").read_text()
    assert "type uniformFixedValue;" in velocity
    assert "type expression;" in velocity
