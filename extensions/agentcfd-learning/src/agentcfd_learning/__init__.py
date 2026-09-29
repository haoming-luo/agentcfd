"""Framework-neutral adapters for verified AgentCFD scientific datasets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agentcfd.interoperability import open_scientific_dataset


@dataclass(frozen=True, slots=True)
class LearningPartition:
    """One immutable, normalized dataset partition."""

    case_ids: tuple[str, ...]
    inputs: tuple[tuple[float, ...], ...]
    outputs: tuple[tuple[float, ...], ...]

    def to_numpy(self):
        """Return NumPy matrices only when the optional dependency is requested."""

        try:
            import numpy as np
        except ImportError as error:  # pragma: no cover - optional environment
            raise RuntimeError(
                "NumPy is optional; install `agentcfd-learning[arrays]`."
            ) from error
        return np.asarray(self.inputs, dtype=float), np.asarray(self.outputs, dtype=float)


@dataclass(frozen=True, slots=True)
class LearningBundle:
    """Traceable train/validation batches with no framework-owned tensors."""

    input_names: tuple[str, ...]
    output_names: tuple[str, ...]
    train: LearningPartition
    validation: LearningPartition
    plan: Mapping[str, object]

    def summary(self) -> dict[str, object]:
        return {
            "schema": "agentcfd.learning-bundle/0.1",
            "source": dict(self.plan["source"]),
            "input_names": list(self.input_names),
            "output_names": list(self.output_names),
            "train_count": len(self.train.case_ids),
            "validation_count": len(self.validation.case_ids),
            "normalization": dict(self.plan["normalization"]),
            "split": dict(self.plan["split"]),
        }


def _normalized_rows(
    samples: tuple[Mapping[str, object], ...],
    *,
    names: tuple[str, ...],
    source: str,
    statistics: tuple[Mapping[str, object], ...],
) -> tuple[tuple[float, ...], ...]:
    by_name = {str(record["name"]): record for record in statistics}
    return tuple(
        tuple(
            (float(sample[source][name]) - float(by_name[name]["offset"]))
            / float(by_name[name]["scale"])
            for name in names
        )
        for sample in samples
    )


def prepare_dataset(
    directory: str | Path,
    *,
    validation_fraction: float = 0.2,
    seed: int = 0,
) -> LearningBundle:
    """Verify and prepare leakage-safe scalar learning partitions."""

    reader = open_scientific_dataset(directory)
    plan = reader.training_plan(
        validation_fraction=validation_fraction,
        seed=seed,
    )
    samples = tuple(reader.iter_samples())
    by_id = {str(sample["case_id"]): sample for sample in samples}
    split = plan["split"]
    normalization = plan["normalization"]

    def partition(ids: list[str]) -> LearningPartition:
        selected = tuple(by_id[case_id] for case_id in ids)
        return LearningPartition(
            case_ids=tuple(ids),
            inputs=_normalized_rows(
                selected,
                names=reader.input_names,
                source="inputs",
                statistics=tuple(normalization["inputs"]),
            ),
            outputs=_normalized_rows(
                selected,
                names=reader.output_names,
                source="outputs",
                statistics=tuple(normalization["outputs"]),
            ),
        )

    return LearningBundle(
        input_names=reader.input_names,
        output_names=reader.output_names,
        train=partition(split["train_case_ids"]),
        validation=partition(split["validation_case_ids"]),
        plan=plan,
    )


def agentfem_sample_records(
    directory: str | Path,
) -> tuple[dict[str, object], ...]:
    """Return records accepted directly by ``agentfem.datasets.Sample``.

    The bridge intentionally returns plain mappings and never imports AgentFEM,
    so the two products remain independently installable.
    """

    reader = open_scientific_dataset(directory)
    return tuple(
        {
            "case_id": sample["case_id"],
            "inputs": dict(sample["inputs"]),
            "outputs": dict(sample["outputs"]),
            "provenance": dict(sample["provenance"]),
            "artifacts": dict(sample["artifacts"]),
        }
        for sample in reader.iter_samples()
    )


class _ScientificDatasetExtension:
    def descriptor(self) -> dict[str, object]:
        return {
            "schema": "agentcfd.extension/1",
            "kind": "learning",
            "name": "scientific-dataset",
            "capabilities": [
                "verified-scalar-dataset",
                "training-only-normalization",
                "deterministic-train-validation-split",
                "framework-neutral-batches",
                "agentfem-sample-records",
            ],
        }

    prepare = staticmethod(prepare_dataset)
    agentfem_records = staticmethod(agentfem_sample_records)


extension = _ScientificDatasetExtension()


__all__ = [
    "LearningBundle",
    "LearningPartition",
    "agentfem_sample_records",
    "extension",
    "prepare_dataset",
]
