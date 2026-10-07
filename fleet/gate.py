"""Approval gate: destructive / outward-facing tool calls pause for the owner.

Default deny: a request that is not approved within the timeout is denied.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from .db import APPROVED, PENDING, RUNNING, WAITING, Store
from .tools import Tool

OnRequest = Callable[[int, str, dict], Awaitable[None]]


class ApprovalGate:
    def __init__(
        self,
        store: Store,
        task_id: int,
        timeout_s: float = 900.0,
        poll_s: float = 1.0,
        on_request: OnRequest | None = None,
    ) -> None:
        self.store, self.task_id = store, task_id
        self.timeout_s, self.poll_s = timeout_s, poll_s
        self.on_request = on_request

    async def authorize(self, tool: Tool, tool_input: dict) -> tuple[bool, str]:
        if not await tool.requires_approval(tool_input):
            return True, ""
        aid = self.store.create_approval(self.task_id, tool.name, tool_input)
        self.store.set_status(self.task_id, WAITING)
        try:
            if self.on_request:
                await self.on_request(aid, tool.name, tool_input)
            deadline = time.monotonic() + self.timeout_s
            while True:
                appr = self.store.get_approval(aid)
                if appr and appr.status != PENDING:
                    ok = appr.status == APPROVED
                    if ok:
                        return True, ""
                    why = f": {appr.reason}" if appr.reason else ""
                    return False, f"denied by owner{why}"
                if time.monotonic() >= deadline:
                    self.store.decide_approval(aid, False, "timed out")
                    return False, "approval timed out (default deny)"
                await asyncio.sleep(self.poll_s)
        finally:
            self.store.set_status(self.task_id, RUNNING)
