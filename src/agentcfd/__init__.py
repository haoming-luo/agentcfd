"""AgentCFD public engineering language."""

from . import benchmarks, boundaries, capabilities, contracts, engineering, fluids, geometry, interoperability, outputs, procedures, properties, providers, studies, verification
from ._version import __version__
from .model import Model, Step
from .results import Artifact, Check, FieldRecord, History, Quantity, SimulationResult, read_result_record

__all__ = [
    "Artifact",
    "Check",
    "FieldRecord",
    "History",
    "Model",
    "Quantity",
    "SimulationResult",
    "Step",
    "__version__",
    "benchmarks",
    "boundaries",
    "capabilities",
    "contracts",
    "engineering",
    "fluids",
    "geometry",
    "interoperability",
    "outputs",
    "procedures",
    "properties",
    "providers",
    "studies",
    "verification",
    "read_result_record",
]
