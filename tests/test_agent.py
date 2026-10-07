import pytest
from conftest import FakeClient, reply, text, tool_use

from fleet.agent import AgentError, Budget, BudgetExceeded, run_agent
from fleet.gate import ApprovalGate
from fleet.pricing import cost_usd
from fleet.review import parse_verdict
from fleet.tools import Registry, Tool, _obj, resolve_in


def make_registry(log):
    async def safe(i):
        log.append(("safe", i))
        return "ok"

    async def danger(i):
        log.append(("danger", i))
        return "sent"

    return Registry(
        [
            Tool("safe", "s", _obj({}, []), safe, approval=False),
            Tool("danger", "d", _obj({"x": {"type": "string"}}, ["x"]), danger, approval=True),
        ]
    )


async def run(client, reg, gate=None, budget=None, **kw):
    return await run_agent(
        client,
        model="claude-opus-5-5",
        system="s",
        prompt="p",
        registry=reg,
        gate=gate,
        budget=budget or Budget(10.0),
        **kw,
    )


async def test_loop_runs_tool_and_returns_text():
    log = []
    c = FakeClient([reply(tool_use("t1", "safe")), reply(text("all done"))])
    assert await run(c, make_registry(log)) == "all done"
    assert log == [("safe", {})]
    last = c.calls[1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["tool_use_id"] == "t1"
    assert c.calls[0]["model"] == "claude-opus-5-5"
    assert c.calls[0]["thinking"] == {"type": "adaptive"}


async def test_parallel_tool_results_in_one_message():
    c = FakeClient([reply(tool_use("a", "safe"), tool_use("b", "safe")), reply(text("ok"))])
    await run(c, make_registry([]))
    assert len(c.calls[1]["messages"][-1]["content"]) == 2


async def test_unknown_tool_is_error_result():
    c = FakeClient([reply(tool_use("a", "nope")), reply(text("ok"))])
    await run(c, make_registry([]))
    r = c.calls[1]["messages"][-1]["content"][0]
    assert r["is_error"] and "unknown tool" in r["content"]


async def test_destructive_denied_without_gate():
    log = []
    c = FakeClient([reply(tool_use("a", "danger", x="1")), reply(text("ok"))])
    await run(c, make_registry(log), gate=None)
    assert log == []
    assert c.calls[1]["messages"][-1]["content"][0]["is_error"]


async def test_gate_approve_and_deny(store):
    tid = store.add_task("t", 1.0)
    log = []
    reg = make_registry(log)
    gate = ApprovalGate(store, tid, timeout_s=2, poll_s=0.01)
    import asyncio

    async def decide(approve):
        while not store.list_approvals():
            await asyncio.sleep(0.01)
        store.decide_approval(store.list_approvals()[0].id, approve, "nope")

    c = FakeClient([reply(tool_use("a", "danger", x="1")), reply(text("ok"))])
    await asyncio.gather(run(c, reg, gate=gate), decide(True))
    assert log == [("danger", {"x": "1"})]

    log.clear()
    c = FakeClient([reply(tool_use("a", "danger", x="2")), reply(text("ok"))])
    store.decide_approval(1, True)  # no-op, already decided
    await asyncio.gather(run(c, reg, gate=gate), decide(False))
    assert log == []
    assert "denied by owner: nope" in c.calls[1]["messages"][-1]["content"][0]["content"]


async def test_gate_default_deny_on_timeout(store):
    tid = store.add_task("t", 1.0)
    gate = ApprovalGate(store, tid, timeout_s=0.05, poll_s=0.01)
    log = []
    c = FakeClient([reply(tool_use("a", "danger", x="1")), reply(text("ok"))])
    await run(c, make_registry(log), gate=gate)
    assert log == []
    assert store.list_approvals(None)[0].status == "denied"
    assert store.get_task(tid).status == "running"  # restored after wait


async def test_dynamic_approval_classifier_fails_closed():
    def bad(_):
        raise RuntimeError("x")

    async def h(_):
        return "ok"

    assert await Tool("t", "d", _obj({}, []), h, approval=bad).requires_approval({}) is True


async def test_budget_exceeded_stops_loop():
    c = FakeClient(
        [reply(tool_use("a", "safe"), inp=1_000_000, out=1_000_000), reply(text("never"))]
    )
    with pytest.raises(BudgetExceeded):
        await run(c, make_registry([]), budget=Budget(0.01))
    assert len(c.calls) == 1


async def test_stop_reasons_and_max_turns():
    with pytest.raises(AgentError, match="refused"):
        await run(FakeClient([reply(text(""), stop="refusal")]), Registry())
    with pytest.raises(AgentError, match="max_tokens"):
        await run(FakeClient([reply(text("x"), stop="max_tokens")]), Registry())
    c = FakeClient([reply(tool_use(str(i), "safe")) for i in range(3)])
    with pytest.raises(AgentError, match="max_turns"):
        await run(c, make_registry([]), max_turns=3)


def test_cost_and_unknown_model_fallback():
    u = type("U", (), {"input_tokens": 1_000_000, "output_tokens": 1_000_000})()
    assert cost_usd("claude-opus-5-5", u) == pytest.approx(24.0)
    assert cost_usd("mystery", u) == pytest.approx(60.0)


def test_parse_verdict():
    assert parse_verdict("looks good\nVERDICT: PASS") == (True, "")
    ok, why = parse_verdict("VERDICT: FAIL - missing tests")
    assert not ok and why == "missing tests"
    assert parse_verdict("no verdict here")[0] is False


def test_resolve_in_blocks_escape(tmp_path):
    (tmp_path / "ws").mkdir()
    assert resolve_in(tmp_path / "ws", "a/b.txt").parent.name == "a"
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(PermissionError):
            resolve_in(tmp_path / "ws", bad)
