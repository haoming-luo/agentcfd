"""MCP SDK v2 stdio server generated from AgentCFD's operation catalog."""

from __future__ import annotations

import json

from agentcfd import capabilities, contracts, extensions, operations, templates

from .adapter import OperationAdapter


def _handler(adapter: OperationAdapter, operation: operations.Operation):
    names = operations.argument_names(operation)
    if names == ():
        def call() -> dict[str, object]:
            return adapter.invoke(operation.name)

        return call
    if names == ("project",):
        def call(project: str) -> dict[str, object]:
            return adapter.invoke(operation.name, {"project": project})

        return call
    if names == ("project", "template"):
        def call(project: str, template: str) -> dict[str, object]:
            return adapter.invoke(
                operation.name,
                {"project": project, "template": template},
            )

        return call
    raise RuntimeError(
        f"MCP adapter has no typed handler for {operation.name!r} arguments {names!r}."
    )


def create_server(adapter: OperationAdapter | None = None):
    """Create the SDK server lazily so adapter inspection needs no MCP install."""

    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as error:  # pragma: no cover - installation boundary
        raise RuntimeError("Install agentcfd-mcp with its declared MCP dependency.") from error

    selected = adapter or OperationAdapter()
    server = MCPServer(
        "agentcfd",
        title="AgentCFD",
        description="Bounded project lifecycle for AI-native CFD.",
        version="0.1.0a1",
        instructions=(
            "Inspect compatibility and status before mutation. Never infer that a "
            "completed solver run is verified; read the result evidence."
        ),
    )
    for operation in operations.all():
        annotations = ToolAnnotations(**operation.mcp_annotations)
        server.add_tool(
            _handler(selected, operation),
            name=operation.name,
            description=operation.description,
            annotations=annotations,
            meta={"agentcfd": operation.to_dict()},
            structured_output=True,
        )

    resources = {
        "agentcfd://catalog/capabilities": capabilities.as_dict,
        "agentcfd://catalog/templates": templates.as_dict,
        "agentcfd://catalog/extensions": extensions.as_dict,
        "agentcfd://catalog/contracts": contracts.catalog,
    }

    def resource_reader(factory):
        def read() -> str:
            return json.dumps(factory(), indent=2, sort_keys=True)

        return read

    for uri, factory in resources.items():
        server.resource(uri, mime_type="application/json")(
            resource_reader(factory)
        )
    return server


def main() -> None:
    create_server().run(transport="stdio")


__all__ = ["create_server", "main"]
