"""Tool-calling agent loop on the Anthropic Messages API."""

from __future__ import annotations

from dataclasses import dataclass, field

from .gate import ApprovalGate
from .pricing import cost_usd
from .tools import Registry


class BudgetExceeded(Exception):
    pass


class AgentError(Exception):
    pass


@dataclass
class Budget:
    """Spend cap shared by an agent and its reviewer. Checked before every API call and
    charged after, so one call can overshoot the limit by at most its own cost."""

    limit_usd: float
    spent_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    on_charge: object = None  # callable(in_tokens, out_tokens, cost) or None
    log: list[float] = field(default_factory=list)

    def check(self) -> None:
        if self.spent_usd >= self.limit_usd:
            raise BudgetExceeded(f"budget ${self.limit_usd:.2f} exhausted (${self.spent_usd:.4f})")

    def charge(self, model: str, usage: object) -> None:
        c = cost_usd(model, usage)
        i, o = getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
        self.spent_usd += c
        self.input_tokens += i
        self.output_tokens += o
        if self.on_charge:
            self.on_charge(i, o, c)


def _block_text(blocks: list) -> str:
    return "\n".join(b.text for b in blocks if getattr(b, "type", "") == "text").strip()


async def run_agent(
    client,
    *,
    model: str,
    system: str,
    prompt: str,
    registry: Registry,
    gate: ApprovalGate | None,
    budget: Budget,
    max_turns: int = 30,
    max_tokens: int = 16000,
) -> str:
    """Run until the model stops calling tools. Returns the final text."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    tools = registry.specs()
    for _ in range(max_turns):
        budget.check()
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
        )
        if tools:
            kwargs["tools"] = tools
        resp = await client.messages.create(**kwargs)
        budget.charge(model, resp.usage)
        if resp.stop_reason == "refusal":
            raise AgentError("model refused the request")
        if resp.stop_reason == "max_tokens":
            raise AgentError("response hit max_tokens")
        calls = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if resp.stop_reason != "tool_use" or not calls:
            return _block_text(resp.content)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for call in calls:  # sequential: approvals are interactive and ordered
            results.append(await _run_tool(registry, gate, call))
        messages.append({"role": "user", "content": results})
    raise AgentError(f"exceeded max_turns={max_turns}")


async def _run_tool(registry: Registry, gate: ApprovalGate | None, call) -> dict:
    def err(msg: str) -> dict:
        return {"type": "tool_result", "tool_use_id": call.id, "content": msg, "is_error": True}

    tool = registry.get(call.name)
    if tool is None:
        return err(f"unknown tool {call.name!r}")
    if gate is None:
        # No gate configured: only tools that never need approval may run.
        if await tool.requires_approval(call.input):
            return err("denied: no approval gate available")
    else:
        ok, why = await gate.authorize(tool, call.input)
        if not ok:
            return err(why)
    try:
        out = await tool.handler(call.input)
    except Exception as e:
        return err(f"{type(e).__name__}: {e}")
    return {"type": "tool_result", "tool_use_id": call.id, "content": out}
