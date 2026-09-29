"""Small, typed operation surface for agents, GUIs, and future MCP adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import shlex


_PLACEHOLDER = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Operation:
    """One bounded product operation with explicit side-effect semantics."""

    name: str
    command: str
    effect: str
    description: str
    idempotent: bool
    retry_policy: str
    approval_policy: str
    expected_artifacts: tuple[str, ...] = ()
    destructive: bool = False
    starts_solver: bool = False
    supports_preview: bool = False
    output_contract: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("Operation names must use letters, numbers, or underscores.")
        if self.effect not in {"read", "write", "execute"}:
            raise ValueError("Operation effect must be read, write, or execute.")
        if self.retry_policy not in {
            "safe",
            "state-dependent",
            "do-not-retry-automatically",
        }:
            raise ValueError("Operation retry_policy is unsupported.")
        if self.approval_policy not in {
            "none",
            "user-write",
            "user-execution",
            "user-destructive",
        }:
            raise ValueError("Operation approval_policy is unsupported.")
        if not self.command.startswith("agentcfd ") or not self.description.strip():
            raise ValueError("Operations require one AgentCFD command and description.")
        if any(not item.strip() for item in self.expected_artifacts):
            raise ValueError("Expected artifact roles must be non-empty strings.")
        if len(self.expected_artifacts) != len(set(self.expected_artifacts)):
            raise ValueError("Expected artifact roles must be unique.")
        if self.effect == "read" and (self.destructive or self.starts_solver):
            raise ValueError("Read operations cannot be destructive or start a solver.")
        if self.effect == "read" and self.approval_policy != "none":
            raise ValueError("Read operations cannot require mutation approval.")
        if self.starts_solver and self.effect != "execute":
            raise ValueError("Solver-starting operations must use effect='execute'.")
        if self.starts_solver and self.approval_policy != "user-execution":
            raise ValueError("Solver-starting operations require user-execution approval.")
        if self.destructive and self.effect != "write":
            raise ValueError("Destructive operations must use effect='write'.")
        if self.destructive and self.approval_policy != "user-destructive":
            raise ValueError("Destructive operations require user-destructive approval.")

    @property
    def mcp_annotations(self) -> dict[str, bool]:
        """Return conservative hints matching the MCP tool-annotation vocabulary."""

        return {
            "readOnlyHint": self.effect == "read",
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.starts_solver,
        }

    def to_dict(self) -> dict[str, object]:
        record = asdict(self)
        record["expected_artifacts"] = list(self.expected_artifacts)
        return {**record, "mcp_annotations": self.mcp_annotations}


_OPERATIONS = (
    Operation(
        "discover_capabilities",
        "agentcfd capabilities --json",
        "read",
        "Discover public APIs, providers, commands, workflow stages, and maturity.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="capability-catalog.schema.json",
    ),
    Operation(
        "discover_templates",
        "agentcfd templates --json",
        "read",
        "Discover executable project starting points and their required inputs.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="template-catalog.schema.json",
    ),
    Operation(
        "discover_extensions",
        "agentcfd extensions --json",
        "read",
        "Discover compatible optional packages without importing their code.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="extension-catalog.schema.json",
    ),
    Operation(
        "discover_mcp_manifest",
        "agentcfd mcp-manifest --json",
        "read",
        "Describe bounded MCP resources and tools generated from product contracts.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="mcp-manifest.schema.json",
    ),
    Operation(
        "inspect_compatibility",
        "agentcfd compatibility PROJECT --json",
        "read",
        "Inspect project schema compatibility without importing or changing project code.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="project-compatibility.schema.json",
    ),
    Operation(
        "create_project",
        "agentcfd init PROJECT --template TEMPLATE --json",
        "write",
        "Create a new readable project without overwriting an existing directory.",
        idempotent=False,
        retry_policy="state-dependent",
        approval_policy="user-write",
        expected_artifacts=("agentcfd.toml", "case.py"),
        supports_preview=False,
        output_contract="project-initialization.schema.json",
    ),
    Operation(
        "inspect_project",
        "agentcfd project PROJECT --json",
        "read",
        "Read one bounded project snapshot without opening field payloads.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="project-snapshot.schema.json",
    ),
    Operation(
        "check_project",
        "agentcfd check PROJECT --json",
        "read",
        "Validate intent and provider compatibility without starting a solver.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
    ),
    Operation(
        "plan_project",
        "agentcfd plan PROJECT --json",
        "read",
        "Resolve the deterministic provider, output, storage, and execution plan.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="solution-plan.schema.json",
    ),
    Operation(
        "inspect_status",
        "agentcfd status PROJECT --json",
        "read",
        "Read lifecycle state, progress, and one recommended next action.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="project-status.schema.json",
    ),
    Operation(
        "inspect_result",
        "agentcfd result PROJECT --json",
        "read",
        "Read compact quantities and evidence without loading field arrays.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="result-summary.schema.json",
    ),
    Operation(
        "run_project",
        "agentcfd run project PROJECT --json",
        "execute",
        "Start the resolved provider and publish a checked project result.",
        idempotent=False,
        retry_policy="do-not-retry-automatically",
        approval_policy="user-execution",
        expected_artifacts=("run.json", "result.json", "summary.json"),
        starts_solver=True,
    ),
    Operation(
        "resume_project",
        "agentcfd resume PROJECT --json",
        "execute",
        "Resume the identity-matched recoverable provider run.",
        idempotent=False,
        retry_policy="state-dependent",
        approval_policy="user-execution",
        expected_artifacts=("run.json", "result.json", "summary.json"),
        starts_solver=True,
    ),
    Operation(
        "diagnose_project",
        "agentcfd diagnose PROJECT --json",
        "read",
        "Classify a bounded failure and return evidence-linked repair guidance.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="project-diagnosis.schema.json",
    ),
    Operation(
        "verify_project",
        "agentcfd verify project PROJECT --json",
        "read",
        "Verify result identity, artifacts, and available field bundle integrity.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        output_contract="project-verification.schema.json",
    ),
    Operation(
        "preview_cleanup",
        "agentcfd clean PROJECT --json",
        "read",
        "Preview disposable provider data without deleting any file.",
        idempotent=True,
        retry_policy="safe",
        approval_policy="none",
        supports_preview=True,
        output_contract="project-clean.schema.json",
    ),
    Operation(
        "apply_cleanup",
        "agentcfd clean PROJECT --apply --json",
        "write",
        "Delete only the previewed disposable provider data and preserve protected runs.",
        idempotent=True,
        retry_policy="state-dependent",
        approval_policy="user-destructive",
        destructive=True,
        supports_preview=True,
        output_contract="project-clean.schema.json",
    ),
)


def all() -> tuple[Operation, ...]:
    return _OPERATIONS


def get(name: str) -> Operation:
    selected = str(name).strip()
    for operation in _OPERATIONS:
        if operation.name == selected:
            return operation
    raise ValueError(
        f"Unknown AgentCFD operation {name!r}; "
        f"expected one of {tuple(item.name for item in _OPERATIONS)!r}."
    )


def argument_names(operation: str | Operation) -> tuple[str, ...]:
    """Return the declared uppercase placeholders as normalized input names."""

    selected = get(operation) if isinstance(operation, str) else operation
    return tuple(
        dict.fromkeys(
            token.lower()
            for token in shlex.split(selected.command)
            if _PLACEHOLDER.fullmatch(token)
        )
    )


def argv(name: str, /, **arguments: str) -> tuple[str, ...]:
    """Render one catalog command as argv without invoking a shell."""

    operation = get(name)
    tokens = shlex.split(operation.command)
    placeholders = argument_names(operation)
    missing = [
        placeholder for placeholder in placeholders if placeholder not in arguments
    ]
    extra = sorted(set(arguments) - set(placeholders))
    invalid = [
        placeholder
        for placeholder in placeholders
        if not isinstance(arguments.get(placeholder), str)
        or not str(arguments[placeholder]).strip()
        or str(arguments[placeholder]).startswith("-")
        or "\x00" in str(arguments[placeholder])
    ]
    if missing or extra or invalid:
        raise ValueError(
            f"Operation {name!r} arguments are invalid: missing={missing!r}, "
            f"extra={extra!r}, invalid={invalid!r}."
        )
    rendered = [
        arguments[token.lower()] if _PLACEHOLDER.fullmatch(token) else token
        for token in tokens[1:]
    ]
    return tuple(rendered)


def as_dict() -> dict[str, object]:
    return {
        "schema": "agentcfd.operation-catalog/0.1",
        "operations": [operation.to_dict() for operation in _OPERATIONS],
    }


__all__ = ["Operation", "all", "argument_names", "argv", "as_dict", "get"]
