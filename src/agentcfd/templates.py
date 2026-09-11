"""Discoverable project templates for people, agents, and frontends."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ProjectTemplate:
    """One truthful, executable project starting point."""

    id: str
    title: str
    purpose: str
    maturity: str
    providers: tuple[str, ...]
    default_provider: str
    geometry_mode: str
    physics: tuple[str, ...]
    outputs: tuple[str, ...]
    requires: tuple[str, ...]
    limitations: tuple[str, ...]
    create_command: str
    request_contract: str
    request_required: tuple[str, ...]
    request_optional: tuple[str, ...] = ()
    request_exactly_one: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.title or not self.purpose:
            raise ValueError("Template id, title, and purpose are required.")
        if self.maturity not in {"release", "experimental"}:
            raise ValueError("Template maturity must be release or experimental.")
        if not self.providers or self.default_provider not in self.providers:
            raise ValueError("Template default provider must be a supported provider.")
        if self.geometry_mode not in {"generated", "imported"}:
            raise ValueError("Template geometry mode must be generated or imported.")
        for values in (
            self.providers,
            self.physics,
            self.outputs,
            self.requires,
            self.limitations,
            self.request_required,
            self.request_optional,
        ):
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("Template string collections cannot contain blanks.")
        if self.request_contract != "project-creation-request.schema.json":
            raise ValueError("Templates must use the stable project creation contract.")
        if len(set(self.request_required)) != len(self.request_required):
            raise ValueError("Template required request fields must be unique.")
        if set(self.request_required) & set(self.request_optional):
            raise ValueError("Template request fields cannot be required and optional.")
        for group in self.request_exactly_one:
            if len(group) < 2 or any(not value.strip() for value in group):
                raise ValueError("Exactly-one request groups require named alternatives.")

    def to_dict(self) -> dict[str, object]:
        record = asdict(self)
        for name in (
            "providers",
            "physics",
            "outputs",
            "requires",
            "limitations",
            "request_required",
            "request_optional",
        ):
            record[name] = list(record[name])
        record["request_exactly_one"] = [
            list(group) for group in self.request_exactly_one
        ]
        return record


_TEMPLATES = (
    ProjectTemplate(
        id="industrial-pipe",
        title="Industrial circular pipe",
        purpose=(
            "Start a readable incompressible circular-pipe project for pressure drop, "
            "flow, campaign, and workflow validation."
        ),
        maturity="release",
        providers=("reference", "openfoam"),
        default_provider="reference",
        geometry_mode="generated",
        physics=("steady", "incompressible", "laminar", "isothermal"),
        outputs=(
            "fluid.velocity",
            "fluid.pressure",
            "flow.pressure_drop",
            "flow.mass_balance",
        ),
        requires=("diameter", "length", "fluid", "mean inlet velocity"),
        limitations=(
            "The reference provider is the fully developed Hagen-Poiseuille solution.",
            "The OpenFOAM path is bounded to the validated laminar circular-pipe slice.",
        ),
        create_command="agentcfd init PROJECT --template industrial-pipe",
        request_contract="project-creation-request.schema.json",
        request_required=("schema", "template", "provider"),
    ),
    ProjectTemplate(
        id="heated-pipe",
        title="Constant-property heated pipe",
        purpose=(
            "Start a laminar circular-pipe energy study with prescribed wall heat "
            "flux and explicit mass/energy closure."
        ),
        maturity="experimental",
        providers=("openfoam",),
        default_provider="openfoam",
        geometry_mode="generated",
        physics=(
            "steady",
            "incompressible",
            "laminar",
            "constant-property heat transport",
        ),
        outputs=(
            "fluid.velocity",
            "fluid.pressure",
            "thermal.temperature",
            "thermal.energy_balance",
        ),
        requires=(
            "diameter",
            "length",
            "density",
            "viscosity",
            "specific heat",
            "thermal conductivity",
            "inlet temperature",
            "wall heat flux",
        ),
        limitations=(
            "No buoyancy, radiation, phase change, conjugate wall, or steam properties.",
            "Properties are constant and must be justified over the temperature range.",
        ),
        create_command=(
            "agentcfd init PROJECT --template heated-pipe --provider openfoam"
        ),
        request_contract="project-creation-request.schema.json",
        request_required=("schema", "template", "provider"),
        request_optional=("parameters",),
    ),
    ProjectTemplate(
        id="baffle-channel",
        title="Transient bottom-baffle channel",
        purpose=(
            "Start a short asymmetric channel with a bottom-attached baffle for "
            "transient wake visualization and post-processing workflows."
        ),
        maturity="experimental",
        providers=("openfoam",),
        default_provider="openfoam",
        geometry_mode="generated",
        physics=("transient", "incompressible", "laminar", "isothermal"),
        outputs=(
            "fluid.velocity",
            "fluid.pressure",
            "wake slice",
            "streamlines",
            "time-resolved XDMF/HDF5",
        ),
        requires=("channel dimensions", "baffle dimensions", "inlet velocity"),
        limitations=(
            "One rectangular channel and one bottom-attached thin baffle only.",
            "It is a workflow demonstration, not a validated turbulent bluff-body benchmark.",
        ),
        create_command=(
            "agentcfd init PROJECT --template baffle-channel --provider openfoam"
        ),
        request_contract="project-creation-request.schema.json",
        request_required=("schema", "template", "provider"),
    ),
    ProjectTemplate(
        id="industrial-elbow",
        title="Generated industrial circular elbow",
        purpose=(
            "Create and own a parameterized 90-degree circular-elbow fluid volume "
            "for pressure-loss, outlet-quality, campaign, and field workflows."
        ),
        maturity="experimental",
        providers=("openfoam",),
        default_provider="openfoam",
        geometry_mode="generated",
        physics=(
            "steady",
            "incompressible",
            "laminar or k-omega SST",
            "isothermal",
        ),
        outputs=(
            "fluid.velocity",
            "fluid.pressure",
            "flow.pressure_loss",
            "flow.uniformity",
            "XDMF/HDF5 fields",
        ),
        requires=(
            "diameter",
            "bend radius",
            "inlet and outlet straight lengths",
            "exactly one inlet control",
            "mesh size and hard cell budget",
        ),
        limitations=(
            "The generated domain is one uniform-diameter circular 90-degree elbow.",
            "Prism layers, roughness, thermal flow, and reacting flow are pending.",
            "The generated mesh path is verified; elbow-loss accuracy and grid sensitivity remain open.",
        ),
        create_command=(
            "agentcfd init PROJECT --template industrial-elbow "
            "--diameter-m 0.1 --bend-radius-m 0.15 --inlet-length-m 0.3 "
            "--outlet-length-m 0.4 --inlet-velocity-m-s 1 0 0"
        ),
        request_contract="project-creation-request.schema.json",
        request_required=(
            "schema",
            "template",
            "provider",
            "generated_geometry",
            "mesh",
        ),
        request_exactly_one=(
            (
                "inlet_velocity_m_s",
                "inlet_mass_flow_kg_s",
                "inlet_total_gauge_pressure_pa",
            ),
        ),
    ),
    ProjectTemplate(
        id="imported-internal-flow",
        title="Imported industrial internal flow",
        purpose=(
            "Own an STL/OBJ fluid volume for ducts, bends, splitters, manifolds, "
            "and equipment pressure-loss or flow-distribution studies."
        ),
        maturity="experimental",
        providers=("openfoam",),
        default_provider="openfoam",
        geometry_mode="imported",
        physics=(
            "steady",
            "incompressible",
            "laminar or k-omega SST",
            "isothermal",
        ),
        outputs=(
            "fluid.velocity",
            "fluid.pressure",
            "flow.pressure_loss",
            "flow.uniformity",
            "flow.distribution for multiple outlets",
        ),
        requires=(
            "closed named STL/OBJ fluid volume",
            "geometry unit",
            "confirmed boundary roles",
            "explicit component review when more than one surface shell exists",
            "interior point",
            "exactly one inlet control",
            "base cell size",
            "maximum cell budget",
        ),
        limitations=(
            "STEP/IGES tessellation, prism layers, thermal flow, moving meshes, and reacting flow are pending.",
            "RANS remains diagnostic until geometry-specific grid and validation evidence pass.",
        ),
        create_command=(
            "agentcfd init PROJECT --template imported-internal-flow --provider "
            "openfoam --geometry GEOMETRY --unit UNIT --roles ROLES_JSON "
            "--interior-point-m X Y Z --inlet-velocity-m-s UX UY UZ "
            "--base-size-m SIZE --maximum-cells COUNT"
        ),
        request_contract="project-creation-request.schema.json",
        request_required=(
            "schema",
            "template",
            "provider",
            "geometry",
            "interior_point_m",
            "mesh",
        ),
        request_optional=("flow_distribution",),
        request_exactly_one=(
            (
                "inlet_velocity_m_s",
                "inlet_mass_flow_kg_s",
                "inlet_total_gauge_pressure_pa",
            ),
        ),
    ),
)

_BY_ID = {template.id: template for template in _TEMPLATES}
_DEFAULT_TEMPLATE_ID = "industrial-pipe"


def all() -> tuple[ProjectTemplate, ...]:
    """Return every built-in template in stable product order."""

    return _TEMPLATES


def ids() -> tuple[str, ...]:
    """Return template ids accepted by project initialization."""

    return tuple(template.id for template in _TEMPLATES)


def default() -> ProjectTemplate:
    """Return the default beginner-safe project template."""

    return _BY_ID[_DEFAULT_TEMPLATE_ID]


def get(template_id: str) -> ProjectTemplate:
    """Resolve one template or fail with the discoverable valid ids."""

    selected = str(template_id).strip()
    try:
        return _BY_ID[selected]
    except KeyError as error:
        raise ValueError(
            f"Unknown project template {template_id!r}; available={ids()}."
        ) from error


def as_dict() -> dict[str, object]:
    """Return the versioned template catalog for agents and frontends."""

    return {
        "schema": "agentcfd.template-catalog/0.1",
        "default_template": _DEFAULT_TEMPLATE_ID,
        "templates": [template.to_dict() for template in _TEMPLATES],
    }


__all__ = ["ProjectTemplate", "all", "as_dict", "default", "get", "ids"]
