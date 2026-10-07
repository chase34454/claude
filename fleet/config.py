from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Config:
    db_path: str = "fleet.db"
    model: str = DEFAULT_MODEL
    review_model: str = DEFAULT_MODEL
    workspace: Path = Path("workspace")
    concurrency: int = 4
    default_budget_usd: float = 1.0
    approval_timeout_s: float = 900.0
    approval_poll_s: float = 1.0
    queue_poll_s: float = 2.0
    max_turns: int = 30
    max_tokens: int = 16000
    browser_allow_private: bool = False
    browser_no_sandbox: bool = False
    chromium_path: str | None = None
    claude_bin: str = "claude"

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        model = env.get("FLEET_MODEL") or DEFAULT_MODEL
        return cls(
            db_path=env.get("FLEET_DB", "fleet.db"),
            model=model,
            review_model=env.get("FLEET_REVIEW_MODEL") or model,
            workspace=Path(env.get("FLEET_WORKSPACE", "workspace")),
            concurrency=int(env.get("FLEET_CONCURRENCY", "4")),
            default_budget_usd=float(env.get("FLEET_DEFAULT_BUDGET_USD", "1.0")),
            approval_timeout_s=float(env.get("FLEET_APPROVAL_TIMEOUT_S", "900")),
            max_turns=int(env.get("FLEET_MAX_TURNS", "30")),
            browser_allow_private=_flag("FLEET_BROWSER_ALLOW_PRIVATE"),
            browser_no_sandbox=_flag("FLEET_BROWSER_NO_SANDBOX"),
            chromium_path=env.get("FLEET_CHROMIUM_PATH") or None,
            claude_bin=env.get("FLEET_CLAUDE_BIN", "claude"),
        )
