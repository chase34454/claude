import asyncio

from conftest import FakeClient, reply, text, tool_use

from fleet.alerts import CRITICAL, FakeAlerter
from fleet.worker import run_pool


def passing(result="the answer"):
    return [reply(text(result)), reply(text("VERDICT: PASS - fine"))]


async def test_task_done_after_review(store, cfg):
    tid = store.add_task("do it", 5.0)
    client = FakeClient(passing())
    alerts = FakeAlerter()
    await run_pool(store, client, alerts, cfg, drain=True)
    t = store.get_task(tid)
    assert t.status == "done" and t.result == "the answer" and t.cost_usd > 0
    assert t.input_tokens == 200
    assert alerts.sent == []
    assert "<result>" in client.calls[1]["messages"][0]["content"]


async def test_review_reject_retries_with_feedback_then_passes(store, cfg):
    tid = store.add_task("do it", 5.0, max_attempts=2)
    client = FakeClient(
        [
            reply(text("bad")),
            reply(text("VERDICT: FAIL - add detail")),
            reply(text("better")),
            reply(text("VERDICT: PASS")),
        ]
    )
    await run_pool(store, client, FakeAlerter(), cfg, drain=True)
    t = store.get_task(tid)
    assert t.status == "done" and t.attempts == 2 and t.result == "better"
    assert "add detail" in client.calls[2]["messages"][0]["content"]


async def test_exhausted_attempts_fail_and_alert(store, cfg):
    tid = store.add_task("do it", 5.0, max_attempts=1)
    client = FakeClient([reply(text("bad")), reply(text("VERDICT: FAIL - wrong"))])
    alerts = FakeAlerter()
    await run_pool(store, client, alerts, cfg, drain=True)
    assert store.get_task(tid).status == "failed"
    assert alerts.sent and "FAILED" in alerts.sent[0][1]


async def test_budget_cap_fails_without_retry_and_alerts_critical(store, cfg):
    tid = store.add_task("do it", 0.001, max_attempts=3)
    client = FakeClient([reply(text("x"), inp=1_000_000, out=1_000_000)])
    alerts = FakeAlerter()
    await run_pool(store, client, alerts, cfg, drain=True)
    t = store.get_task(tid)
    assert t.status == "failed" and t.attempts == 1 and "budget" in t.error
    assert alerts.sent[0][0] == CRITICAL


async def test_api_error_retries(store, cfg):
    tid = store.add_task("do it", 5.0, max_attempts=2)
    client = FakeClient([RuntimeError("network"), *passing()])
    await run_pool(store, client, FakeAlerter(), cfg, drain=True)
    t = store.get_task(tid)
    assert t.status == "done" and t.attempts == 2


async def test_approval_flow_end_to_end(store, cfg):
    (cfg.workspace).mkdir(parents=True)
    tid = store.add_task("write a file", 5.0)
    client = FakeClient(
        [
            reply(tool_use("w", "write_file", path="out.txt", content="hi")),
            reply(text("written")),
            reply(text("VERDICT: PASS")),
        ]
    )
    alerts = FakeAlerter()

    async def owner():
        while not store.list_approvals():
            await asyncio.sleep(0.01)
        assert store.get_task(tid).status == "waiting_approval"
        store.decide_approval(store.list_approvals()[0].id, True)

    await asyncio.gather(run_pool(store, client, alerts, cfg, drain=True), owner())
    assert (cfg.workspace / "out.txt").read_text() == "hi"
    assert store.get_task(tid).status == "done"
    assert "needs approval" in alerts.sent[0][1]


async def test_parallel_workers_process_all(store, cfg):
    for i in range(4):
        store.add_task(f"t{i}", 5.0)

    def respond(kw):
        if kw["system"].startswith("You are a strict reviewer"):
            return reply(text("VERDICT: PASS"))
        return reply(text("ok"))

    client = FakeClient([respond] * 8)
    await run_pool(store, client, FakeAlerter(), cfg, drain=True)
    assert [t.status for t in store.list_tasks()] == ["done"] * 4
