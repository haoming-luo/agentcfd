"""Single source of truth for AgentCFD's discoverable product language.

The inventory intentionally imports no numerical or provider dependency.  The
Python package, CLI, documentation, agent clients, and future GUI/MCP surfaces
must discover the same workflow modules, facade methods, commands, and stages.
"""

from __future__ import annotations


CORE_WORKFLOW_MODULES = (
    "studies",
    "geometry",
    "fluids",
    "boundaries",
    "meshing",
    "procedures",
    "outputs",
    "projects",
    "results",
)

ADVANCED_WORKFLOW_MODULES = (
    "regions",
    "initialization",
    "engineering",
    "zero_d",
    "properties",
    "verification",
    "data_exchange",
    "interoperability",
    "templates",
    "operations",
)

EXPERT_WORKFLOW_MODULES = (
    "providers",
    "contracts",
    "capabilities",
    "diagnostics",
    "provenance",
    "archives",
    "geometry_generation",
    "geometry_io",
    "postprocessing",
    "extensions",
    "mcp",
    "parameters",
    "benchmarks",
    "licensing",
)

CORE_MODEL_API = (
    "boundaries",
    "sections",
    "validate",
    "step",
)

ADVANCED_MODEL_API = (
    "boundary_conditions",
    "section_definitions",
    "to_dict",
    "fingerprint",
)

CORE_STEP_API = (
    "run",
    "observation_catalog",
)

ADVANCED_STEP_API = (
    "to_dict",
    "fingerprint",
)

CORE_PROJECT_API = (
    "plan",
    "run",
    "resume",
    "status",
    "result_summary",
    "inspect",
    "snapshot",
    "actions",
    "verify",
    "doctor",
)

ADVANCED_PROJECT_API = (
    "parameter_contract",
    "parameter_set",
    "load_step",
    "observations",
    "sync_generated_geometry",
    "campaign_index",
    "scientific_sample",
    "export_scientific_sample",
    "export_campaign_dataset",
    "export_campaign_csv",
    "campaign_operating_map",
    "export_campaign_operating_map",
    "plan_campaign",
    "run_campaign",
    "promote_campaign_run",
    "compact_campaign_run",
    "recovery",
    "logs",
    "diagnose",
    "performance",
    "storage",
    "clean",
)

EXPERT_PROJECT_API = (
    "archive_plan",
    "export_archive",
)

CLI_COMMANDS = (
    "doctor",
    "init",
    "check",
    "plan",
    "observations",
    "inspect",
    "geometry-create",
    "geometry-sync",
    "geometry-check",
    "geometry-normalize",
    "mesh",
    "status",
    "project",
    "actions",
    "params",
    "result",
    "watch",
    "logs",
    "diagnose",
    "resume",
    "storage",
    "performance",
    "campaigns",
    "sweep",
    "promote",
    "compact",
    "archive",
    "restore",
    "clean",
    "view",
    "capabilities",
    "extensions",
    "mcp-manifest",
    "benchmarks",
    "templates",
    "contracts",
    "dataset",
    "licenses",
    "export",
    "calculate",
    "properties",
    "demo",
    "prepare",
    "run",
    "verify",
)

MACHINE_COMMANDS = {
    "environment_check": "agentcfd doctor --json",
    "capabilities": "agentcfd capabilities --json",
    "extensions": "agentcfd extensions --json",
    "mcp_manifest": "agentcfd mcp-manifest --json",
    "templates": "agentcfd templates --json",
    "contracts": "agentcfd contracts --json",
    "project_check": "agentcfd check . --json",
    "project_plan": "agentcfd plan . --json",
    "project_status": "agentcfd status . --json",
    "project_snapshot": "agentcfd project . --json",
    "project_actions": "agentcfd actions . --json",
    "project_result": "agentcfd result . --json",
    "project_run": "agentcfd run project . --json",
    "project_verify": "agentcfd verify project . --json",
}

WORKFLOW_STAGES = (
    "study",
    "model",
    "geometry_mesh_and_regions",
    "fluid_and_properties",
    "boundaries_and_sources",
    "procedure_and_outputs",
    "plan",
    "solve",
    "result_and_verification",
)

CAPABILITIES_SCHEMA_VERSION = "0.2"


def _all(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


PUBLIC_WORKFLOW_MODULES = _all(
    CORE_WORKFLOW_MODULES,
    ADVANCED_WORKFLOW_MODULES,
    EXPERT_WORKFLOW_MODULES,
)

PUBLIC_MODEL_API = _all(CORE_MODEL_API, ADVANCED_MODEL_API)
PUBLIC_STEP_API = _all(CORE_STEP_API, ADVANCED_STEP_API)
PUBLIC_PROJECT_API = _all(
    CORE_PROJECT_API,
    ADVANCED_PROJECT_API,
    EXPERT_PROJECT_API,
)


def workflow_modules(level: str = "all") -> tuple[str, ...]:
    """Return public workflow modules at one progressive-disclosure level."""

    return _level(
        level,
        {
            "core": CORE_WORKFLOW_MODULES,
            "advanced": ADVANCED_WORKFLOW_MODULES,
            "expert": EXPERT_WORKFLOW_MODULES,
            "all": PUBLIC_WORKFLOW_MODULES,
        },
        noun="public_api",
    )


def facade_methods(facade: str, level: str = "all") -> tuple[str, ...]:
    """Return discoverable methods for the Model, Step, or Project facade."""

    selected_facade = str(facade).lower().strip()
    try:
        levels = {
            "model": {
                "core": CORE_MODEL_API,
                "advanced": ADVANCED_MODEL_API,
                "all": PUBLIC_MODEL_API,
            },
            "step": {
                "core": CORE_STEP_API,
                "advanced": ADVANCED_STEP_API,
                "all": PUBLIC_STEP_API,
            },
            "project": {
                "core": CORE_PROJECT_API,
                "advanced": ADVANCED_PROJECT_API,
                "expert": EXPERT_PROJECT_API,
                "all": PUBLIC_PROJECT_API,
            },
        }[selected_facade]
    except KeyError as exc:
        raise ValueError("facade must be model, step, or project.") from exc
    return _level(level, levels, noun=f"{selected_facade}_api")


def facade_method_contract(
    facade: str = "all", level: str = "all"
) -> tuple[dict[str, object], ...]:
    """Return progressive-disclosure metadata for public facade methods."""

    selected = str(facade).lower().strip()
    facades = ("model", "step", "project") if selected == "all" else (selected,)
    records: list[dict[str, object]] = []
    for facade_name in facades:
        for name in facade_methods(facade_name, level):
            tier = _method_tier(facade_name, name)
            records.append(
                {
                    "facade": facade_name,
                    "name": name,
                    "tier": tier,
                    "lifecycle": "recommended" if tier == "core" else "supported",
                }
            )
    return tuple(records)


def _method_tier(facade: str, name: str) -> str:
    if name in facade_methods(facade, "core"):
        return "core"
    if name in facade_methods(facade, "advanced"):
        return "advanced"
    return "expert"


def _level(
    level: str,
    levels: dict[str, tuple[str, ...]],
    *,
    noun: str,
) -> tuple[str, ...]:
    selected = str(level).lower().replace("-", "_").strip()
    try:
        return levels[selected]
    except KeyError as exc:
        choices = ", ".join((*tuple(levels)[:-1], f"or {tuple(levels)[-1]}"))
        raise ValueError(f"{noun} level must be {choices}.") from exc
