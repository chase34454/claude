"""Tool registry. Every tool must declare whether it needs owner approval.

`approval` is True (always), False (never) or a callable taking the tool input and
returning a bool (sync or async) for tools whose risk depends on the arguments.
There is intentionally no default: forgetting to decide is a TypeError, and a
classifier that raises is treated as "needs approval".
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Handler = Callable[[dict], Awaitable[str | list[dict]]]
Approval = bool | Callable[[dict], "bool | Awaitable[bool]"]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    approval: Approval

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    async def requires_approval(self, tool_input: dict) -> bool:
        if isinstance(self.approval, bool):
            return self.approval
        try:
            res = self.approval(tool_input)
            if inspect.isawaitable(res):
                res = await res
            return bool(res)
        except Exception:
            return True  # fail closed


class Registry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.add(t)

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)


def _obj(props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def resolve_in(root: Path, rel: str) -> Path:
    """Resolve rel inside root, rejecting traversal and symlink escapes."""
    root = root.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise PermissionError(f"path escapes workspace: {rel}")
    return p


def file_tools(workspace: Path) -> list[Tool]:
    workspace.mkdir(parents=True, exist_ok=True)
    path_prop = {"path": {"type": "string", "description": "Path relative to the workspace"}}

    async def read_file(i: dict) -> str:
        return resolve_in(workspace, i["path"]).read_text()[:50_000]

    async def list_dir(i: dict) -> str:
        p = resolve_in(workspace, i.get("path", "."))
        return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()))

    async def write_file(i: dict) -> str:
        p = resolve_in(workspace, i["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(i["content"])
        return f"wrote {len(i['content'])} chars to {i['path']}"

    async def delete_file(i: dict) -> str:
        p = resolve_in(workspace, i["path"])
        if p.is_dir():
            raise IsADirectoryError(i["path"])
        p.unlink()
        return f"deleted {i['path']}"

    return [
        Tool(
            "read_file",
            "Read a text file from the workspace.",
            _obj(path_prop, ["path"]),
            read_file,
            approval=False,
        ),
        Tool(
            "list_dir",
            "List a workspace directory.",
            _obj({"path": {"type": "string"}}, []),
            list_dir,
            approval=False,
        ),
        Tool(
            "write_file",
            "Create or overwrite a workspace file.",
            _obj({**path_prop, "content": {"type": "string"}}, ["path", "content"]),
            write_file,
            approval=True,
        ),
        Tool(
            "delete_file",
            "Delete a workspace file.",
            _obj(path_prop, ["path"]),
            delete_file,
            approval=True,
        ),
    ]
