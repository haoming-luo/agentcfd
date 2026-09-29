"""AgentCFD public engineering language."""

from . import (
    benchmarks,
    archives,
    boundaries,
    capabilities,
    contracts,
    data_exchange,
    diagnostics,
    engineering,
    fluids,
    geometry,
    geometry_generation,
    geometry_io,
    initialization,
    interoperability,
    licensing,
    meshing,
    operations,
    outputs,
    parameters,
    postprocessing,
    provenance,
    procedures,
    projects,
    properties,
    providers,
    regions,
    results,
    studies,
    templates,
    verification,
    zero_d,
)
from ._api_contract import (
    ADVANCED_WORKFLOW_MODULES,
    CORE_WORKFLOW_MODULES,
    EXPERT_WORKFLOW_MODULES,
    PUBLIC_WORKFLOW_MODULES,
    workflow_modules as _workflow_modules,
)
from ._version import __version__
from .archives import restore_project_archive, verify_project_archive
from .geometry_io import normalize_geometry_regions, plan_region_normalization
from .geometry_generation import plan_circular_elbow_stl, write_circular_elbow_stl
from .model import Model, Step
from .interoperability import ScientificDatasetReader, open_scientific_dataset
from .projects import open_project
from .results import (
    Artifact,
    Check,
    FieldRecord,
    History,
    Quantity,
    SimulationResult,
    read_result_record,
)


def public_api(level: str = "all") -> tuple[str, ...]:
    """Return public modules at a progressive-disclosure level."""

    return _workflow_modules(level)


__all__ = [
    "Artifact",
    "Check",
    "FieldRecord",
    "History",
    "Model",
    "Quantity",
    "SimulationResult",
    "ScientificDatasetReader",
    "Step",
    "__version__",
    "PUBLIC_WORKFLOW_MODULES",
    "CORE_WORKFLOW_MODULES",
    "ADVANCED_WORKFLOW_MODULES",
    "EXPERT_WORKFLOW_MODULES",
    "benchmarks",
    "archives",
    "boundaries",
    "capabilities",
    "contracts",
    "data_exchange",
    "diagnostics",
    "engineering",
    "fluids",
    "geometry",
    "geometry_generation",
    "geometry_io",
    "initialization",
    "interoperability",
    "licensing",
    "meshing",
    "operations",
    "normalize_geometry_regions",
    "outputs",
    "open_project",
    "open_scientific_dataset",
    "parameters",
    "postprocessing",
    "provenance",
    "plan_region_normalization",
    "plan_circular_elbow_stl",
    "procedures",
    "properties",
    "projects",
    "providers",
    "public_api",
    "regions",
    "results",
    "restore_project_archive",
    "studies",
    "templates",
    "verification",
    "verify_project_archive",
    "write_circular_elbow_stl",
    "read_result_record",
    "zero_d",
]
