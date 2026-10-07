"""Orchestrate Claude Code sessions by running the `claude` CLI headlessly.

Uses `claude -p <prompt> --output-format json` in a chosen working directory. A Claude
Code session can edit files and run commands, so the tool always needs approval.
This module is tested against a fake executable, not against the real CLI.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .tools import Tool, _obj, resolve_in


def claude_code_tool(claude_bin: str, workspace: Path, timeout_s: float = 1800) -> Tool:
    async def run(i: dict) -> str:
        cwd = resolve_in(workspace, i.get("cwd", "."))
        proc = await asyncio.create_subprocess_exec(
            claude_bin,
            "-p",
            i["prompt"],
            "--output-format",
            "json",
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"claude exited {proc.returncode}: {err.decode()[:500]}")
        try:
            return str(json.loads(out).get("result", out.decode()))[:20_000]
        except (ValueError, AttributeError):
            return out.decode()[:20_000]

    return Tool(
        "claude_code_session",
        "Run a headless Claude Code session in a workspace directory and return its result.",
        _obj({"prompt": {"type": "string"}, "cwd": {"type": "string"}}, ["prompt"]),
        run,
        approval=True,
    )
