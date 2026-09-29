from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


class _Dataset:
    input_names = ("velocity",)
    output_names = ("pressure",)

    def iter_samples(self):
        yield {
            "case_id": "train",
            "inputs": {"velocity": 2.0},
            "outputs": {"pressure": 10.0},
            "provenance": {"solver": "reference"},
            "artifacts": {"result": "result.json"},
        }
        yield {
            "case_id": "validation",
            "inputs": {"velocity": 6.0},
            "outputs": {"pressure": 30.0},
            "provenance": {"solver": "reference"},
            "artifacts": {"result": "result.json"},
        }

    def training_plan(self, *, validation_fraction, seed):
        assert validation_fraction == 0.5
        assert seed == 17
        return {
            "source": {"samples_sha256": "a" * 64},
            "normalization": {
                "fitted_on": "training-partition-only",
                "inputs": [{"name": "velocity", "offset": 2.0, "scale": 1.0}],
                "outputs": [{"name": "pressure", "offset": 10.0, "scale": 1.0}],
            },
            "split": {
                "train_case_ids": ["train"],
                "validation_case_ids": ["validation"],
            },
        }


def test_learning_extension_prepares_framework_neutral_batches(monkeypatch):
    monkeypatch.syspath_prepend(
        str(ROOT / "extensions" / "agentcfd-learning" / "src")
    )
    import agentcfd_learning

    monkeypatch.setattr(agentcfd_learning, "open_scientific_dataset", lambda _: _Dataset())
    bundle = agentcfd_learning.prepare_dataset(
        "ignored", validation_fraction=0.5, seed=17
    )

    assert bundle.train.case_ids == ("train",)
    assert bundle.train.inputs == ((0.0,),)
    assert bundle.validation.inputs == ((4.0,),)
    assert bundle.validation.outputs == ((20.0,),)
    assert bundle.summary()["normalization"]["fitted_on"] == (
        "training-partition-only"
    )
    descriptor = agentcfd_learning.extension.descriptor()
    assert descriptor["name"] == "scientific-dataset"
    assert descriptor["kind"] == "learning"
    records = agentcfd_learning.agentfem_sample_records("ignored")
    assert set(records[0]) == {
        "case_id",
        "inputs",
        "outputs",
        "provenance",
        "artifacts",
    }


def test_mcp_adapter_enforces_permissions_roots_and_shell_free_argv(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(ROOT / "extensions" / "agentcfd-mcp" / "src"))
    from agentcfd_mcp import AdapterPolicy, InvocationError, OperationAdapter

    calls = []

    def runner(argv, timeout):
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"schema": "test.result/0.1"}),
            stderr="",
        )

    adapter = OperationAdapter(
        AdapterPolicy(roots=(tmp_path,)),
        runner=runner,
    )
    project = tmp_path / "project with spaces"
    result = adapter.invoke("inspect_compatibility", {"project": str(project)})
    assert result["schema"] == "test.result/0.1"
    assert calls[0][0] == (
        "compatibility",
        str(project.resolve()),
        "--json",
    )

    with pytest.raises(InvocationError, match="write permission"):
        adapter.invoke(
            "create_project",
            {"project": str(project), "template": "industrial-pipe"},
        )
    with pytest.raises(InvocationError, match="outside"):
        adapter.invoke("inspect_compatibility", {"project": "/outside/project"})
