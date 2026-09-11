import json
import os
import subprocess
from pathlib import Path

import jsonschema
import pytest

from agentcfd import contracts, data_exchange


meshio = pytest.importorskip("meshio")
np = pytest.importorskip("numpy")
pytest.importorskip("h5py")


def _write_frame(
    root: Path,
    time: float,
    scale: float,
    *,
    vtk_directory: str = "VTK",
) -> Path:
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
    frame = root / vtk_directory / f"case_{time}" / "internal.vtu"
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
        formats=("xdmf", "npz"),
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
    assert manifest["storage"]["source_vtu_policy"] == "preserve"
    assert manifest["storage"]["consumed_source_vtu_bytes"] == 0
    assert (case / "VTK" / "case_0" / "internal.vtu").is_file()
    assert (case / "VTK" / "case_10" / "internal.vtu").is_file()

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


def test_field_bundle_binds_portable_fields_to_source_mesh_identity(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)
    mesh_sha256 = "a" * 64

    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        convert=False,
        density=1000.0,
        source={"mesh_sha256": mesh_sha256, "case_directory": None},
    )

    manifest = json.loads(bundle.manifest.read_text())
    assert manifest["mesh"]["source_sha256"] == mesh_sha256
    assert manifest["source"]["mesh_sha256"] == mesh_sha256
    assert manifest["source"]["case_directory"] is None

    import h5py

    with h5py.File(bundle.hdf5, "r") as h5:
        assert h5.attrs["source_mesh_sha256"] == mesh_sha256
    assert data_exchange.verify_field_bundle(bundle.directory)["verified"] is True


def test_field_bundle_rejects_malformed_source_mesh_identity(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)

    with pytest.raises(ValueError, match="mesh_sha256"):
        data_exchange.export_openfoam_case(
            case,
            tmp_path / "bundle",
            convert=False,
            source={"mesh_sha256": "not-a-digest"},
        )


def test_openfoam_export_filters_restart_times_from_public_frames(tmp_path):
    case = tmp_path / "case"
    for time in range(5):
        _write_frame(case, time, float(time + 1))

    interval = data_exchange.export_openfoam_case(
        case,
        tmp_path / "interval",
        convert=False,
        density=1000.0,
        include_initial=False,
        time_interval=2.0,
    )
    final = data_exchange.export_openfoam_case(
        case,
        tmp_path / "final",
        convert=False,
        density=1000.0,
        latest_only=True,
    )

    assert interval.times == (2.0, 4.0)
    assert final.times == (4.0,)


def test_agentfem_field_sample_bridge_is_pickle_free(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)
    _write_frame(case, 10, 2.0)
    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        convert=False,
        density=1000.0,
        formats=("xdmf", "npz"),
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
    if os.name != "nt":
        assert point_sample.stat().st_mode & 0o777 == 0o644
    with np.load(cell_sample, allow_pickle=False) as sample:
        np.testing.assert_allclose(sample["coordinates"], [[0.5, 0.5, 0.5]])
        np.testing.assert_allclose(sample["values"], [2000.0])


def test_visualization_profile_selects_only_requested_point_fields(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)

    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        convert=False,
        density=1000.0,
        profile="visualization",
        fields=("fluid.velocity", "fluid.pressure"),
        formats=("xdmf", "npz"),
    )

    manifest = json.loads(bundle.manifest.read_text())
    assert manifest["output_selection"] == {
        "profile": "visualization",
        "associations": ["point"],
        "requested_fields": ["fluid.velocity", "fluid.pressure"],
    }
    assert {
        (record["name"], record["association"])
        for record in manifest["fields"]
    } == {("fluid.velocity", "point"), ("fluid.pressure", "point")}
    with np.load(bundle.npz, allow_pickle=False) as arrays:
        field_keys = {key for key in arrays.files if key.startswith(("point__", "cell__"))}
        assert field_keys == {
            "point__fluid_velocity_point",
            "point__fluid_pressure_point",
        }


def test_field_selection_fails_closed_on_unknown_name(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)

    with pytest.raises(ValueError, match="unavailable"):
        data_exchange.export_openfoam_case(
            case,
            tmp_path / "bundle",
            convert=False,
            profile="native",
            fields=("fluid.not-a-field",),
        )


def test_xdmf_only_bundle_omits_npz_without_losing_verification(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)
    _write_frame(case, 10, 2.0)

    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        convert=False,
        profile="visualization",
        fields=("fluid.velocity",),
    )

    assert bundle.npz is None
    assert not (bundle.directory / "fields.npz").exists()
    manifest = json.loads(bundle.manifest.read_text())
    jsonschema.Draft202012Validator(contracts.load("field-bundle.schema.json")).validate(
        manifest
    )
    assert manifest["formats"] == {
        "xdmf": "fields.xdmf",
        "hdf5": "fields.h5",
    }
    assert manifest["storage"]["compression"] == "gzip"
    assert manifest["storage"]["hdf5_write_strategy"] == "single-pass-direct"
    assert manifest["storage"]["temporary_hdf5_copy_bytes"] == 0
    assert manifest["storage"]["actual_portable_bytes"] > 0
    assert not bundle.hdf5.with_suffix(".h5.repack").exists()

    import h5py

    with h5py.File(bundle.hdf5, "r") as h5:
        compressed = [
            item.compression
            for item in h5.values()
            if isinstance(item, h5py.Dataset) and item.ndim > 0
        ]
    assert compressed and all(value == "gzip" for value in compressed)
    assert manifest["arrays"] == []
    assert data_exchange.verify_field_bundle(bundle.directory)["formats"] == [
        "hdf5",
        "xdmf",
    ]

    with pytest.raises(ValueError, match="NPZ-enabled parent bundle"):
        data_exchange.export_agentfem_field_sample(
            bundle.directory,
            tmp_path / "sample.npz",
            field="fluid.velocity",
        )


def test_uncompressed_hdf5_is_written_directly_without_chunk_filters(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)

    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        convert=False,
        fields=("fluid.velocity",),
        compression="none",
    )

    import h5py

    with h5py.File(bundle.hdf5, "r") as h5:
        datasets = [item for item in h5.values() if isinstance(item, h5py.Dataset)]
        assert datasets
        assert all(item.compression is None for item in datasets)
        assert all(item.chunks is None for item in datasets)
    manifest = json.loads(bundle.manifest.read_text())
    assert manifest["storage"]["hdf5_write_strategy"] == "single-pass-direct"
    assert manifest["storage"]["temporary_hdf5_copy_bytes"] == 0


def test_portable_export_fails_before_writing_when_budget_is_too_small(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)
    _write_frame(case, 10, 2.0)

    with pytest.raises(Exception, match="exceeds its declared storage budget"):
        data_exchange.export_openfoam_case(
            case,
            tmp_path / "bundle",
            convert=False,
            maximum_bytes=1024,
        )

    assert not (tmp_path / "bundle").exists()


def test_openfoam_series_is_numerically_sorted(tmp_path):
    case = tmp_path / "case"
    late = _write_frame(case, 100, 1.0)
    early = _write_frame(case, 20, 1.0)

    assert data_exchange.openfoam_vtu_series(case) == (early, late)


def test_openfoam_series_uses_physical_times_from_vtm_index(tmp_path):
    case = tmp_path / "case"
    first = _write_frame(case, 72, 1.0)
    second = _write_frame(case, 145, 2.0)
    (case / "VTK" / "case.vtm.series").write_text(
        json.dumps(
            {
                "file-series-version": "1.0",
                "files": [
                    {"name": "case_145.vtm", "time": 0.4},
                    {"name": "case_72.vtm", "time": 0.2},
                ],
            }
        )
    )

    files = data_exchange.openfoam_vtu_series(case)
    assert files == (first, second)
    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        convert=False,
        axis={
            "name": "time",
            "unit": "s",
            "physical_time": True,
            "description": "Physical time.",
        },
    )
    assert bundle.times == (0.2, 0.4)


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


def test_conversion_log_accumulates_bounded_batches(tmp_path, monkeypatch):
    case = tmp_path / "case"
    case.mkdir()
    _write_frame(case, 0, 1.0)
    calls = 0

    def fake_run(argv, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"converted batch {calls}\n",
            stderr="",
        )

    monkeypatch.setattr("agentcfd.data_exchange.shutil.which", lambda name: "/docker")
    monkeypatch.setattr("agentcfd.data_exchange.subprocess.run", fake_run)

    data_exchange.convert_openfoam_fields(
        case,
        container_image="opencfd/openfoam-run:2606",
        _log_header="native times 0,1,2,3",
    )
    data_exchange.convert_openfoam_fields(
        case,
        container_image="opencfd/openfoam-run:2606",
        _append_log=True,
        _log_header="native times 4,5",
    )

    assert (case / "log.foamToVTK").read_text() == (
        "=== native times 0,1,2,3 ===\n"
        "converted batch 1\n"
        "=== native times 4,5 ===\n"
        "converted batch 2\n"
    )


def test_conversion_limits_temporary_vtk_to_requested_times_and_fields(
    tmp_path, monkeypatch
):
    case = tmp_path / "case"
    case.mkdir()
    _write_frame(case, 0, 1.0)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="converted\n", stderr="")

    monkeypatch.setattr("agentcfd.data_exchange.shutil.which", lambda name: "/docker")
    monkeypatch.setattr("agentcfd.data_exchange.subprocess.run", fake_run)

    data_exchange.convert_openfoam_fields(
        case,
        container_image="opencfd/openfoam-run:2606",
        times=("0.1", "0.2"),
        fields=("U", "p"),
    )

    argv = calls[0]
    assert argv[argv.index("-time") + 1] == "0.1,0.2"
    assert argv[argv.index("-fields") + 1] == "(U p)"
    assert "shell" not in argv


def test_named_conversion_isolates_output_from_existing_vtk(tmp_path, monkeypatch):
    case = tmp_path / "case"
    case.mkdir()
    existing = _write_frame(case, 99, 9.0)
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        output_name = argv[argv.index("-name") + 1]
        _write_frame(case, 1, 1.0, vtk_directory=output_name)
        return subprocess.CompletedProcess(argv, 0, stdout="converted\n", stderr="")

    monkeypatch.setattr("agentcfd.data_exchange.shutil.which", lambda name: "/docker")
    monkeypatch.setattr("agentcfd.data_exchange.subprocess.run", fake_run)

    frames = data_exchange.convert_openfoam_fields(
        case,
        container_image="opencfd/openfoam-run:2606",
        output_name=".agentcfd-vtk-test",
    )

    argv = calls[0]
    assert argv[argv.index("-name") + 1] == ".agentcfd-vtk-test"
    assert "-overwrite" in argv
    assert frames[0].is_relative_to(case / ".agentcfd-vtk-test")
    assert existing.is_file()
    with pytest.raises(ValueError, match="one relative path component"):
        data_exchange.convert_openfoam_fields(case, output_name="../outside")
    with pytest.raises(ValueError, match="must start with '.agentcfd-vtk-'"):
        data_exchange.convert_openfoam_fields(case, output_name="VTK")

    occupied = case / ".agentcfd-vtk-occupied"
    occupied.mkdir()
    (occupied / "user-owned.keep").touch()
    with pytest.raises(FileExistsError, match="is not empty"):
        data_exchange.convert_openfoam_fields(
            case,
            container_image="opencfd/openfoam-run:2606",
            output_name=occupied.name,
        )


def test_isolated_conversion_staging_is_removed_when_bundle_export_fails(
    tmp_path, monkeypatch
):
    case = tmp_path / "case"
    case.mkdir()
    (case / "1").mkdir()
    existing = _write_frame(case, 99, 9.0)
    staged_roots = []

    def fake_convert(case_directory, **kwargs):
        output_name = kwargs["output_name"]
        staged_roots.append(Path(case_directory) / output_name)
        frame = _write_frame(
            Path(case_directory), 1, 1.0, vtk_directory=output_name
        )
        return (frame,)

    def fail_export(vtu_files, *_args, **_kwargs):
        next(iter(vtu_files))
        raise RuntimeError("synthetic portable publication failure")

    monkeypatch.setattr(
        "agentcfd.data_exchange.convert_openfoam_fields", fake_convert
    )
    monkeypatch.setattr("agentcfd.data_exchange.export_vtu_series", fail_export)

    with pytest.raises(RuntimeError, match="synthetic portable publication failure"):
        data_exchange.export_openfoam_case(case, tmp_path / "bundle")

    assert len(staged_roots) == 1
    assert not staged_roots[0].exists()
    assert not (tmp_path / "bundle").exists()
    assert not list(tmp_path.glob(".bundle.agentcfd-publish-*"))
    assert existing.is_file()


def test_native_time_selection_happens_before_openfoam_conversion(tmp_path, monkeypatch):
    case = tmp_path / "case"
    case.mkdir()
    for name in ("0", "0.1", "0.2", "0.3"):
        (case / name).mkdir()
    _write_frame(case, 0, 1.0)
    _write_frame(case, 0.2, 2.0)
    calls = []

    def fake_convert(*args, **kwargs):
        calls.append(kwargs)
        output_name = kwargs["output_name"]
        selected_time = float(kwargs["times"][0])
        _write_frame(
            case,
            selected_time,
            1.0 + selected_time,
            vtk_directory=output_name,
        )
        return data_exchange.openfoam_vtu_series(case, directory=output_name)

    monkeypatch.setattr("agentcfd.data_exchange.convert_openfoam_fields", fake_convert)

    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        density=1000.0,
        include_initial=False,
        time_interval=0.2,
        fields=("fluid.velocity", "fluid.pressure"),
    )

    assert [call["times"] for call in calls] == [("0.2",)]
    assert calls[0]["fields"] == ("U", "p")
    manifest = json.loads(bundle.manifest.read_text())
    assert manifest["source"]["field_conversion"] == {
        "staging": "bounded-batch-isolated-vtu",
        "native_times": ["0.2"],
        "native_fields": ["U", "p"],
        "boundary_fields_included": False,
        "temporary_vtk_policy": "bounded-convert-write-release",
        "conversion_invocation_count": 1,
        "maximum_managed_vtu_frames": 1,
    }
    assert manifest["storage"]["source_vtu_policy"] == "delete-after-frame"
    assert manifest["storage"]["source_vtu_pipeline"] == (
        "bounded-convert-write-release"
    )
    assert manifest["storage"]["maximum_managed_source_vtu_frames"] == 1
    assert manifest["storage"]["conversion_invocation_count"] == 1
    assert manifest["storage"]["bundle_publication_strategy"] == (
        "same-parent-atomic-rename"
    )
    assert manifest["storage"]["consumed_source_vtu_bytes"] > 0
    assert not list(case.glob(".agentcfd-vtk-*"))
    assert (case / "VTK" / "case_0" / "internal.vtu").is_file()
    assert (case / "VTK" / "case_0.2" / "internal.vtu").is_file()


def test_openfoam_conversion_uses_bounded_vtu_micro_batches(tmp_path, monkeypatch):
    case = tmp_path / "case"
    case.mkdir()
    for name in ("0.1", "0.2", "0.3", "0.4", "0.5", "0.6"):
        (case / name).mkdir()
    calls: list[tuple[str, ...]] = []
    simultaneous_staging_counts: list[int] = []
    generated_frame_counts: list[int] = []
    progress: list[dict[str, object]] = []

    def fake_convert(*_args, **kwargs):
        time_names = kwargs["times"]
        calls.append(time_names)
        simultaneous_staging_counts.append(
            len(list(case.glob(".agentcfd-vtk-frame-*")))
        )
        output_name = kwargs["output_name"]
        for time_name in time_names:
            _write_frame(
                case,
                float(time_name),
                1.0 + float(time_name),
                vtk_directory=output_name,
            )
        generated_frame_counts.append(
            len(list((case / output_name).glob("case_*/internal.vtu")))
        )
        return data_exchange.openfoam_vtu_series(case, directory=output_name)

    monkeypatch.setattr("agentcfd.data_exchange.convert_openfoam_fields", fake_convert)

    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        density=1000.0,
        include_initial=False,
        _progress_callback=lambda record: progress.append(dict(record)),
    )

    assert calls == [
        ("0.1", "0.2", "0.3", "0.4"),
        ("0.5", "0.6"),
    ]
    assert simultaneous_staging_counts == [1, 1]
    assert generated_frame_counts == [4, 2]
    assert bundle.times == (0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
    assert not list(case.glob(".agentcfd-vtk-frame-*"))
    manifest = json.loads(bundle.manifest.read_text())
    assert manifest["storage"]["maximum_managed_source_vtu_frames"] == 4
    assert manifest["storage"]["conversion_invocation_count"] == 2
    assert not list(tmp_path.glob(".bundle.agentcfd-publish-*"))
    assert [record["phase"] for record in progress] == [
        "preparing",
        "converting",
        "writing",
        "converting",
        "writing",
        "publishing",
        "complete",
    ]
    assert [record["completed_frames"] for record in progress] == [
        0,
        0,
        0,
        4,
        4,
        6,
        6,
    ]
    assert all(record["total_frames"] == 6 for record in progress)


def test_openfoam_frame_pipeline_uses_one_total_conversion_timeout(
    tmp_path, monkeypatch
):
    case = tmp_path / "case"
    case.mkdir()
    for name in ("0.1", "0.2", "0.3", "0.4", "0.5"):
        (case / name).mkdir()
    calls: list[tuple[str, ...]] = []

    def fake_convert(*_args, **kwargs):
        time_names = kwargs["times"]
        calls.append(time_names)
        frames = tuple(
            _write_frame(
                case,
                float(time_name),
                1.0,
                vtk_directory=kwargs["output_name"],
            )
            for time_name in time_names
        )
        return frames

    clock = iter((0.0, 0.1, 2.0))
    monkeypatch.setattr("agentcfd.data_exchange.convert_openfoam_fields", fake_convert)
    monkeypatch.setattr(
        "agentcfd.data_exchange.time.monotonic",
        lambda: next(clock),
    )

    with pytest.raises(data_exchange.AgentCFDError, match="total timeout"):
        data_exchange.export_openfoam_case(
            case,
            tmp_path / "bundle",
            density=1000.0,
            include_initial=False,
            timeout_seconds=1.0,
        )

    assert calls == [("0.1", "0.2", "0.3", "0.4")]
    assert not list(case.glob(".agentcfd-vtk-frame-*"))


def test_field_export_honors_excluded_initial_frame(tmp_path):
    case = tmp_path / "case"
    _write_frame(case, 0, 1.0)
    _write_frame(case, 10, 2.0)

    bundle = data_exchange.export_openfoam_case(
        case,
        tmp_path / "bundle",
        convert=False,
        include_initial=False,
    )

    assert bundle.times == (10.0,)


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
    output = tmp_path / "bundle"
    output.mkdir()

    with pytest.raises(ValueError, match="non-finite"):
        data_exchange.export_openfoam_case(case, output, convert=False)

    assert output.is_dir()
    assert not list(output.iterdir())
    assert not list(tmp_path.glob(".bundle.agentcfd-publish-*"))
