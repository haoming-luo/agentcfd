"""Machine-readable dependency and solver license boundaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class LicenseComponent:
    name: str
    role: str
    license_expression: str
    relationship: str
    mandatory_runtime: bool
    redistribution_policy: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_COMPONENTS = (
    LicenseComponent(
        name="agentcfd",
        role="AI-native CFD workflow and scientific contracts",
        license_expression="Apache-2.0",
        relationship="core",
        mandatory_runtime=True,
        redistribution_policy="included-with-license-and-notice",
    ),
    LicenseComponent(
        name="numpy",
        role="array hashing, interchange, and NPZ field bundles",
        license_expression="BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        relationship="optional-python-extra:arrays,io",
        mandatory_runtime=False,
        redistribution_policy="retain-bundled-license-files",
    ),
    LicenseComponent(
        name="h5py",
        role="portable HDF5 field storage",
        license_expression="BSD-3-Clause",
        relationship="optional-python-extra:io",
        mandatory_runtime=False,
        redistribution_policy="retain-bundled-license-files-and-review-HDF5-runtime",
    ),
    LicenseComponent(
        name="HDF5",
        role="binary storage runtime used by h5py",
        license_expression="LicenseRef-HDF5",
        relationship="optional-transitive-python-extra:io",
        mandatory_runtime=False,
        redistribution_policy="retain-HDF5-license-and-notice",
    ),
    LicenseComponent(
        name="meshio",
        role="XDMF/HDF5 and mesh-format interchange",
        license_expression="MIT",
        relationship="optional-python-extra:io",
        mandatory_runtime=False,
        redistribution_policy="retain-license-file",
    ),
    LicenseComponent(
        name="CoolProp",
        role="water, steam, and fluid properties",
        license_expression="MIT",
        relationship="optional-python-extra:properties",
        mandatory_runtime=False,
        redistribution_policy="review-version-and-transitives-before-bundling",
    ),
    LicenseComponent(
        name="OpenFOAM",
        role="external finite-volume solver",
        license_expression="GPL-3.0-or-later",
        relationship="user-managed-external-process",
        mandatory_runtime=False,
        redistribution_policy="not-bundled-in-agentcfd-wheel",
    ),
)


def all() -> tuple[LicenseComponent, ...]:
    return _COMPONENTS


def as_dict() -> dict[str, object]:
    return {
        "schema": "agentcfd.license-catalog/0.1",
        "core_has_mandatory_third_party_runtime_dependencies": False,
        "components": [component.to_dict() for component in _COMPONENTS],
    }


__all__ = ["LicenseComponent", "all", "as_dict"]
