"""Dependency-free MCP surface generated from AgentCFD product contracts.

This module describes resources, resource templates, and tools. It is not an
MCP transport server and never executes an operation or imports an extension.
"""

from __future__ import annotations

from . import operations


_ARGUMENT_DESCRIPTIONS = {
    "project": "Path to one bounded AgentCFD project directory.",
    "template": "Stable AgentCFD project template identifier.",
}


def _input_schema(operation: operations.Operation) -> dict[str, object]:
    names = operations.argument_names(operation)
    return {
        "type": "object",
        "properties": {
            name: {
                "type": "string",
                "minLength": 1,
                "description": _ARGUMENT_DESCRIPTIONS.get(
                    name, f"Value substituted for the {name.upper()} placeholder."
                ),
            }
            for name in names
        },
        "required": list(names),
        "additionalProperties": False,
    }


def _tool(operation: operations.Operation) -> dict[str, object]:
    return {
        "name": operation.name,
        "description": operation.description,
        "inputSchema": _input_schema(operation),
        "annotations": dict(operation.mcp_annotations),
        "agentcfd": {
            "command_pattern": operation.command,
            "effect": operation.effect,
            "retry_policy": operation.retry_policy,
            "approval_policy": operation.approval_policy,
            "starts_solver": operation.starts_solver,
            "supports_preview": operation.supports_preview,
            "expected_artifacts": list(operation.expected_artifacts),
            "output_contract": operation.output_contract,
        },
    }


def as_dict() -> dict[str, object]:
    """Return a transport-neutral MCP manifest from existing contracts."""

    catalog_resources = (
        (
            "capabilities",
            "agentcfd://catalog/capabilities",
            "AgentCFD capability, provider, operation, and interface catalog.",
        ),
        (
            "templates",
            "agentcfd://catalog/templates",
            "Executable project templates and required engineering intent.",
        ),
        (
            "extensions",
            "agentcfd://catalog/extensions",
            "Installed optional extension descriptors and compatibility.",
        ),
        (
            "contracts",
            "agentcfd://catalog/contracts",
            "Installed versioned JSON contract inventory.",
        ),
    )
    read_operations = tuple(
        operation
        for operation in operations.all()
        if operation.effect == "read"
        and "project" in operations.argument_names(operation)
    )
    return {
        "schema": "agentcfd.mcp-manifest/0.1",
        "transport": None,
        "execution": "not-implemented-by-manifest",
        "resources": [
            {
                "name": name,
                "uri": uri,
                "description": description,
                "mimeType": "application/json",
            }
            for name, uri, description in catalog_resources
        ],
        "resource_templates": [
            {
                "name": operation.name,
                "uriTemplate": (
                    "agentcfd://project/{project}/operation/" + operation.name
                ),
                "description": operation.description,
                "mimeType": "application/json",
                "command_pattern": operation.command,
                "output_contract": operation.output_contract,
            }
            for operation in read_operations
        ],
        "tools": [_tool(operation) for operation in operations.all()],
        "safety": {
            "arbitrary_shell": False,
            "arbitrary_python": False,
            "extension_code_loaded_during_discovery": False,
            "approval_is_enforced_by": "host-and-operation-adapter",
        },
    }


__all__ = ["as_dict"]
