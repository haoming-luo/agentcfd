"""Bounded operation execution kept independent from MCP transport details."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Mapping

from agentcfd import operations


class InvocationError(RuntimeError):
    """A bounded AgentCFD operation was rejected or failed."""


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class AdapterPolicy:
    roots: tuple[Path, ...]
    allow_writes: bool = False
    allow_execution: bool = False
    allow_destructive: bool = False
    timeout_seconds: float = 3600.0

    def __post_init__(self) -> None:
        roots = tuple(Path(root).expanduser().resolve() for root in self.roots)
        if not roots:
            raise ValueError("At least one AgentCFD MCP root is required.")
        if any(not root.is_dir() for root in roots):
            raise ValueError("Every AgentCFD MCP root must be an existing directory.")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("AgentCFD MCP timeout_seconds must be positive.")
        object.__setattr__(self, "roots", roots)

    @classmethod
    def from_environment(cls) -> "AdapterPolicy":
        encoded = os.environ.get("AGENTCFD_MCP_ROOTS")
        roots = (
            tuple(Path(item) for item in encoded.split(os.pathsep) if item.strip())
            if encoded
            else (Path.cwd(),)
        )
        timeout = float(os.environ.get("AGENTCFD_MCP_TIMEOUT_SECONDS", "3600"))
        return cls(
            roots=roots,
            allow_writes=_enabled("AGENTCFD_MCP_ALLOW_WRITES"),
            allow_execution=_enabled("AGENTCFD_MCP_ALLOW_EXECUTION"),
            allow_destructive=_enabled("AGENTCFD_MCP_ALLOW_DESTRUCTIVE"),
            timeout_seconds=timeout,
        )


Runner = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]


def _run(argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", "agentcfd.cli", *argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


class OperationAdapter:
    """Validate permission, path, and argv contracts before subprocess execution."""

    def __init__(
        self,
        policy: AdapterPolicy | None = None,
        *,
        runner: Runner = _run,
    ) -> None:
        self.policy = policy or AdapterPolicy.from_environment()
        self._runner = runner

    def _authorize(self, operation: operations.Operation) -> None:
        if operation.destructive and not self.policy.allow_destructive:
            raise InvocationError(
                f"Operation {operation.name!r} requires explicit destructive permission."
            )
        if operation.effect == "write" and not self.policy.allow_writes:
            raise InvocationError(
                f"Operation {operation.name!r} requires explicit write permission."
            )
        if operation.effect == "execute" and not self.policy.allow_execution:
            raise InvocationError(
                f"Operation {operation.name!r} requires explicit execution permission."
            )

    def _bounded_project(self, value: str) -> str:
        target = Path(value).expanduser().resolve()
        if not any(target == root or root in target.parents for root in self.policy.roots):
            raise InvocationError("Project path is outside AGENTCFD_MCP_ROOTS.")
        return str(target)

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        operation = operations.get(name)
        self._authorize(operation)
        selected = dict(arguments or {})
        if "project" in selected:
            selected["project"] = self._bounded_project(selected["project"])
        argv = operations.argv(name, **selected)
        try:
            completed = self._runner(argv, self.policy.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise InvocationError(
                f"AgentCFD operation {name!r} exceeded the configured timeout."
            ) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise InvocationError(
                f"AgentCFD operation {name!r} returned non-JSON output."
            ) from error
        if not isinstance(payload, dict):
            raise InvocationError(
                f"AgentCFD operation {name!r} returned a non-object JSON result."
            )
        if completed.returncode != 0:
            message = payload.get("message") or payload.get("error") or completed.stderr
            raise InvocationError(
                f"AgentCFD operation {name!r} failed with code "
                f"{completed.returncode}: {message}"
            )
        return payload


__all__ = ["AdapterPolicy", "InvocationError", "OperationAdapter"]
