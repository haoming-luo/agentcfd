# Reproducible post-processing without duplicate field data

AgentCFD treats visualization and field interrogation as output intent, not a
sequence of unrecorded GUI clicks. A project may declare named plane slices,
scalar contours, seeded streamlines, and compact line profiles beside its field
request:

```python
output=outputs.animation(
    every=0.01,
    views=(
        outputs.slice_view(
            "midplane-vorticity",
            field="fluid.vorticity",
            origin=(0.6, 0.1, 0.05),
            normal=(0.0, 0.0, 1.0),
            component="normal",
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
        outputs.line_profile(
            "centerline-pressure",
            field="fluid.pressure",
            start=(0.05, 0.10, 0.05),
            end=(1.15, 0.10, 0.05),
            samples=121,
        ),
    ),
    layouts=(
        outputs.render_layout(
            "wake-overview",
            views=("midplane-vorticity", "wake-streamlines"),
            columns=2,
            export=outputs.render(size=(1280, 720)),
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
    ├── centerline-pressure.py
    ├── midplane-vorticity.py
    ├── wake-overview.py
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
agentcfd view . --recipe centerline-pressure --batch
agentcfd view . --layout wake-overview --batch
```

The second command starts ParaView with its documented `--script` option. The
script creates the declared filter, connects the portable time series, colors
by the requested array, fits the camera, and saves an editable `.pvsm` state.
The GUI stays available for ordinary exploration; reproducibility does not
remove interactivity.

`--batch` executes a named recipe with ParaView's `pvbatch`, waits for a real
exit status, and returns the output files that exist. This is the
non-interactive path for AI agents and CI. `--launch` remains the interactive
desktop path; the two modes are deliberately mutually exclusive.

## Engineering overviews and vector meaning

`outputs.render_layout()` references existing named render recipes and arranges
two to nine of them in a bounded grid. The generated script opens
`fields.xdmf` once, creates one filter pipeline per panel, shares the time axis,
and saves one editable state plus an optional screenshot or animation. It does
not copy HDF5, and it rejects unknown views or compact line-profile charts
before publication. A layout is part of analysis identity, so changing its
panels or shape makes an existing result visibly stale.

Vector coloring on a slice can be declared as `x`, `y`, `z`, `magnitude`,
`normal`, or `tangential`. `normal` preserves sign along the declared plane
normal—for a 2-D wake midplane this exposes clockwise versus counter-clockwise
vorticity. `tangential` is the magnitude after removing that normal component.
AgentCFD generates these arrays as ParaView Calculator filters at viewing time;
they are not added to the durable field bundle.

A line profile samples the final published frame along its physical line and
writes `name.csv` when the recipe is launched. The CSV writer is restricted to
distance plus the requested scalar array; it does not dump every point/cell
variable and does not create another volume-field file. A vector profile must
make its meaning explicit with `component="x"`, `"y"`, `"z"`, or
`"magnitude"`; omitting that choice fails before ParaView. Scalar fields reject
an irrelevant component. The final two-column header uses canonical AgentCFD
names rather than ParaView's internal point-array suffixes.

```python
outputs.line_profile(
    "wake-speed",
    field="fluid.velocity",
    component="magnitude",
    start=(0.36, 0.01, 0.05),
    end=(1.15, 0.01, 0.05),
    samples=160,
)
```

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

The two-panel layout path was executed against the real 200-frame baffled-
channel result. A 960×360 overview combined signed normal vorticity and
velocity-colored streamlines from one 104 MiB HDF5 payload, producing only a
28 KiB PNG and an editable state. ParaView emitted no layout, reader, filter,
projection, or render errors.

The line-profile batch path was also executed against that result. A 41-sample
request produced 42 CSV rows including the header, exactly two columns
(`distance_m` and `fluid.pressure`), and an editable PVSM. Disabling CSV
metadata and normalizing the small table removed ParaView's otherwise automatic
coordinate columns and provider-facing point suffix.

This design follows ParaView's official filter and automation model:

- the Slice filter reduces dimensionality using an implicit plane, while the
  Contour filter owns scalar isosurfaces:
  <https://docs.paraview.org/en/latest/UsersGuide/filteringData.html>;
- Stream Tracer accepts line or point seeds over a vector field:
  <https://docs.paraview.org/en/latest/UsersGuide/filteringData.html#stream-tracer>;
- Plot Over Line controls the number of samples with `Resolution`, and
  `SaveData` supports selected-array CSV publication:
  <https://docs.paraview.org/en/latest/UsersGuide/filteringData.html#plot-over-line>
  and <https://docs.paraview.org/en/latest/UsersGuide/savingResults.html>;
- `paraview --script` is an official application startup path:
  <https://docs.paraview.org/en/v6.1.0/UsersGuide/commandLineArguments.html>;
- `.pvsm` preserves the visualization pipeline and can be reopened or remapped
  to data under another directory:
  <https://docs.paraview.org/en/latest/UsersGuide/savingResults.html>.
- ParaView's layout API places multiple render views in one tab, and the
  Calculator model provides component, magnitude, and dot-product operations:
  <https://docs.paraview.org/en/latest/UsersGuide/displayingData.html#multiple-views>
  and <https://docs.paraview.org/en/latest/UsersGuide/filteringData.html#calculator>.
