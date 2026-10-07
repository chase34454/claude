from __future__ import annotations

from types import SimpleNamespace

import pytest

from fleet.config import Config
from fleet.db import Store


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id, name, **inp):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=inp)


def reply(*blocks, stop=None, inp=100, out=50):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    usage = SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage)


class FakeClient:
    """Mock Anthropic client: returns scripted responses (or raises them) in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(kw) if callable(item) else item


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


@pytest.fixture
def cfg(tmp_path):
    return Config(
        db_path=str(tmp_path / "t.db"),
        workspace=tmp_path / "ws",
        concurrency=2,
        approval_poll_s=0.01,
        approval_timeout_s=0.5,
        queue_poll_s=0.01,
        browser_allow_private=True,
        browser_no_sandbox=True,
    )
