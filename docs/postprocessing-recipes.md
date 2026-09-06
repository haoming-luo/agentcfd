# Reproducible post-processing without duplicate field data

AgentCFD treats visualization as output intent, not a sequence of unrecorded GUI
clicks. A project may declare named plane slices, scalar contours, and seeded
streamlines beside its field request:

```python
output=outputs.animation(
    every=0.01,
    views=(
        outputs.slice_view(
            "midplane-vorticity",
            field="fluid.vorticity",
            origin=(0.6, 0.1, 0.05),
            normal=(0.0, 0.0, 1.0),
            camera=outputs.camera(
                position=(0.6, 0.1, 2.0),
                focal_point=(0.6, 0.1, 0.05),
                parallel_scale=0.65,
            ),
            export=outputs.render(
                size=(1280, 720),
                screenshot=True,
                animation="png-sequence",
                frame_rate=24,
            ),
        ),
        outputs.contour_view(
            "pressure-levels",
            field="fluid.pressure",
            values=(100.0, 500.0),
        ),
        outputs.streamline_view(
            "wake-streamlines",
            seed_start=(0.02, 0.01, 0.05),
            seed_end=(0.02, 0.19, 0.05),
            seeds=40,
        ),
    ),
)
```

Every referenced canonical field must also be present in `output.fields`; an
incomplete request fails while importing `case.py`, before meshing or solving.
Recipes use visualization point arrays: contours require a scalar and
streamlines require a vector. Native-only cell output or an incompatible field
shape fails before any partial script directory is written, with guidance to
select the `visualization` or `both` portable profile.
The normalized recipes are part of the analysis fingerprint and therefore
cannot silently change after a result is published.

## Published result

After XDMF/HDF5 publication, AgentCFD writes only small text artifacts:

```text
output/
├── fields/fields.xdmf
├── fields/fields.h5
└── postprocess/
    ├── manifest.json
    ├── midplane-vorticity.py
    └── wake-streamlines.py
```

All scripts resolve `../fields/fields.xdmf` relative to themselves. They do not
contain project-specific absolute data paths and do not copy geometry or field
arrays. `manifest.json` records `payload_copies: 0`, canonical and exported
field names, point/cell association, the script, and the state filename that
ParaView creates after launch.

```bash
agentcfd view . --recipe midplane-vorticity
agentcfd view . --recipe midplane-vorticity --launch
```

The second command starts ParaView with its documented `--script` option. The
script creates the declared filter, connects the portable time series, colors
by the requested array, fits the camera, and saves an editable `.pvsm` state.
The GUI stays available for ordinary exploration; reproducibility does not
remove interactivity.

Camera and rendering are explicit opt-ins. Without `camera=`, the generated
script uses ParaView's fitted camera. Without `export=`, launching creates only
the editable `.pvsm` state, so an ordinary run never silently produces hundreds
of images. `outputs.render()` can request a PNG screenshot and either a
`name.%04d.png` sequence or MP4. PNG sequences are the portable default for
automation because they do not assume an encoder; MP4 availability depends on
the ParaView build. The recipe manifest lists every expected
`render_outputs_after_launch` while retaining `payload_copies: 0`.

The screenshot path was executed with ParaView 6.1.1 against a real 50-frame
AgentCFD XDMF/HDF5 result. It produced the requested 640×360 PNG and editable
PVSM without another volume-field copy. Optional OpenVKL device warnings from
that macOS build did not change the successful render exit status.

This design follows ParaView's official filter and automation model:

- the Slice filter reduces dimensionality using an implicit plane, while the
  Contour filter owns scalar isosurfaces:
  <https://docs.paraview.org/en/latest/UsersGuide/filteringData.html>;
- Stream Tracer accepts line or point seeds over a vector field:
  <https://docs.paraview.org/en/latest/UsersGuide/filteringData.html#stream-tracer>;
- `paraview --script` is an official application startup path:
  <https://docs.paraview.org/en/v6.1.0/UsersGuide/commandLineArguments.html>;
- `.pvsm` preserves the visualization pipeline and can be reopened or remapped
  to data under another directory:
  <https://docs.paraview.org/en/latest/UsersGuide/savingResults.html>.

Plot-over-line and multi-view layouts belong in the same text-only layer. They
should be added without creating a second field bundle or coupling the public
API to an OpenFOAM case directory.
