import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from agentcfd import contracts, data_exchange


meshio = pytest.importorskip("meshio")
np = pytest.importorskip("numpy")
pytest.importorskip("h5py")


def _write_frame(root: Path, time: int, scale: float) -> Path:
    points = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 0.0, 1.0],
            [1.0, 1.0, 1.0],
            [0.0, 1.0, 1.0],
        ]
    )
    cells = [("hexahedron", np.asarray([[0, 1, 2, 3, 4, 5, 6, 7]]))]
    frame = root / "VTK" / f"case_{time}" / "internal.vtu"
    frame.parent.mkdir(parents=True)
    meshio.write(
        frame,
        meshio.Mesh(
            points,
            cells,
            point_data={
                "U": np.full((8, 3), scale),
                "p": np.full(8, 2.0 * scale),
            },
            cell_data={
                "U": [np.full((1, 3), scale)],
                "p": [np.full(1, 2.0 * scale)],
            },
        ),
    )
    return frame


def test_xdmf_h5_npz_bundle_round_trip_and_schema(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)
    _write_frame(case, 10, 2.0)

    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        convert=False,
        density=1000.0,
    )

    assert bundle.frame_count == 2
    assert bundle.times == (0.0, 10.0)
    assert bundle.xdmf.is_file()
    assert bundle.hdf5.is_file()
    assert bundle.npz.is_file()
    manifest = json.loads(bundle.manifest.read_text())
    jsonschema.Draft202012Validator(contracts.load("field-bundle.schema.json")).validate(
        manifest
    )
    assert manifest["schema"] == "agentcae.field-bundle"
    assert manifest["axis"]["physical_time"] is False
    assert {field["name"] for field in manifest["fields"]} >= {
        "fluid.velocity",
        "fluid.kinematic_pressure",
        "fluid.pressure",
    }

    with np.load(bundle.npz, allow_pickle=False) as arrays:
        assert arrays["axis"].tolist() == [0.0, 10.0]
        assert arrays["point__fluid_velocity_point"].shape == (2, 8, 3)
        assert arrays["cell__fluid_pressure_cell__0"].shape == (2, 1)
        assert json.loads(str(arrays["metadata_json"]))["npz"]["allow_pickle"] is False

    import h5py

    with h5py.File(bundle.hdf5, "r") as h5:
        assert h5.attrs["agentcae_schema"] == "agentcae.field-bundle"
        assert h5.attrs["schema_version"] == "0.1.0"
        assert h5.attrs["axis_name"] == "provider_step"

    verification = data_exchange.verify_field_bundle(bundle.directory)
    assert verification == {
        "schema": "agentcfd.field-bundle-verification/0.1",
        "verified": True,
        "frame_count": 2,
        "point_count": 8,
        "cell_block_count": 1,
        "formats": ["hdf5", "npz", "xdmf"],
    }


def test_agentfem_field_sample_bridge_is_pickle_free(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)
    _write_frame(case, 10, 2.0)
    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        convert=False,
        density=1000.0,
    )

    point_sample = data_exchange.export_agentfem_field_sample(
        bundle.directory,
        tmp_path / "velocity",
        field="fluid.velocity",
        frame=-1,
    )
    cell_sample = data_exchange.export_agentfem_field_sample(
        bundle.directory,
        tmp_path / "pressure.npz",
        field="fluid.pressure",
        association="cell",
        frame=0,
    )

    with np.load(point_sample, allow_pickle=False) as sample:
        assert set(sample.files) == {
            "coordinates",
            "values",
            "encoding_json",
            "metadata_json",
        }
        assert sample["coordinates"].shape == (8, 3)
        assert sample["values"].shape == (8, 3)
        assert json.loads(str(sample["encoding_json"]))["unit"] == "m/s"
        metadata = json.loads(str(sample["metadata_json"]))
        assert metadata["schema"] == "agentcae.field-sample"
        assert metadata["coordinate_value"] == 10.0
    assert point_sample.stat().st_mode & 0o777 == 0o644
    with np.load(cell_sample, allow_pickle=False) as sample:
        np.testing.assert_allclose(sample["coordinates"], [[0.5, 0.5, 0.5]])
        np.testing.assert_allclose(sample["values"], [2000.0])


def test_openfoam_series_is_numerically_sorted(tmp_path):
    case = tmp_path / "case"
    late = _write_frame(case, 100, 1.0)
    early = _write_frame(case, 20, 1.0)

    assert data_exchange.openfoam_vtu_series(case) == (early, late)


def test_container_conversion_uses_argument_list_and_writes_log(tmp_path, monkeypatch):
    case = tmp_path / "case"
    case.mkdir()
    _write_frame(case, 0, 1.0)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="converted\n", stderr="")

    monkeypatch.setattr("agentcfd.data_exchange.shutil.which", lambda name: "/docker")
    monkeypatch.setattr("agentcfd.data_exchange.subprocess.run", fake_run)

    frames = data_exchange.convert_openfoam_fields(
        case,
        container_image="opencfd/openfoam-run:2606",
    )

    assert len(frames) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == ["/docker", "run", "--rm"]
    assert argv[-4:] == ["foamToVTK", "-case", "/case", "-no-boundary"]
    assert "shell" not in kwargs or kwargs["shell"] is False
    assert (case / "log.foamToVTK").read_text() == "converted\n"


def test_export_rejects_nonempty_destination(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)
    output = tmp_path / "bundle"
    output.mkdir()
    (output / "owned.txt").write_text("preserve")

    with pytest.raises(FileExistsError, match="not empty"):
        data_exchange.export_openfoam_case(case, output, convert=False)

    assert (output / "owned.txt").read_text() == "preserve"


def test_export_rejects_nonfinite_training_fields(tmp_path):
    case = tmp_path / "case"
    frame = _write_frame(case, 0, 1.0)
    mesh = meshio.read(frame)
    mesh.point_data["p"][0] = np.nan
    meshio.write(frame, mesh)

    with pytest.raises(ValueError, match="non-finite"):
        data_exchange.export_openfoam_case(case, tmp_path / "bundle", convert=False)
