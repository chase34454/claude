"""Second-agent verification of a task result. Fails closed."""

from __future__ import annotations

import re

from .agent import Budget, run_agent
from .tools import Registry

REVIEW_SYSTEM = (
    "You are a strict reviewer. You are given a task and the result an agent produced. "
    "Decide whether the result fully and correctly accomplishes the task, with no "
    "fabricated claims. Be brief. Your last line must be exactly 'VERDICT: PASS' or "
    "'VERDICT: FAIL' followed by ' - ' and the reason or the fix needed."
)
_VERDICT = re.compile(r"^VERDICT:\s*(PASS|FAIL)\b\s*(?:-\s*(.*))?$", re.I | re.M)


def parse_verdict(text: str) -> tuple[bool, str]:
    matches = _VERDICT.findall(text)
    if not matches:
        return False, "reviewer returned no verdict"
    verdict, reason = matches[-1]
    passed = verdict.upper() == "PASS"
    return passed, (reason or ("" if passed else text)).strip()


async def review(
    client, *, model: str, task_prompt: str, result: str, budget: Budget, max_tokens: int = 4000
) -> tuple[bool, str]:
    prompt = f"<task>\n{task_prompt}\n</task>\n\n<result>\n{result}\n</result>"
    text = await run_agent(
        client,
        model=model,
        system=REVIEW_SYSTEM,
        prompt=prompt,
        registry=Registry(),
        gate=None,
        budget=budget,
        max_turns=1,
        max_tokens=max_tokens,
    )
    return parse_verdict(text)
