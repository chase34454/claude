import pytest


def test_claim_order_and_attempts(store):
    a = store.add_task("a", 1.0)
    b = store.add_task("b", 1.0)
    t = store.claim_next()
    assert t.id == a and t.status == "running" and t.attempts == 1
    assert store.claim_next().id == b
    assert store.claim_next() is None


def test_retry_then_fail(store):
    tid = store.add_task("x", 1.0, max_attempts=2)
    store.claim_next()
    assert store.fail_attempt(tid, "boom") == "queued"
    store.claim_next()
    assert store.fail_attempt(tid, "boom") == "failed"
    assert store.get_task(tid).error == "boom"


def test_no_retry_flag(store):
    tid = store.add_task("x", 1.0, max_attempts=5)
    store.claim_next()
    assert store.fail_attempt(tid, "budget", retry=False) == "failed"


def test_usage_accumulates(store):
    tid = store.add_task("x", 1.0)
    store.add_usage(tid, 10, 5, 0.25)
    store.add_usage(tid, 1, 1, 0.5)
    t = store.get_task(tid)
    assert (t.input_tokens, t.output_tokens, t.cost_usd) == (11, 6, 0.75)


def test_validation(store):
    with pytest.raises(ValueError):
        store.add_task("x", 0)
    with pytest.raises(ValueError):
        store.add_task("x", 1, max_attempts=0)


def test_approval_decided_once(store):
    tid = store.add_task("x", 1.0)
    aid = store.create_approval(tid, "send_email", {"to": "a@b.c"})
    assert store.list_approvals()[0].tool_input == {"to": "a@b.c"}
    assert store.decide_approval(aid, True)
    assert not store.decide_approval(aid, False)
    assert store.get_approval(aid).status == "approved"


def test_requeue_interrupted(store):
    tid = store.add_task("x", 1.0)
    store.claim_next()
    aid = store.create_approval(tid, "t", {})
    assert store.requeue_interrupted() == 1
    assert store.get_task(tid).status == "queued"
    assert store.get_approval(aid).status == "denied"


def test_concurrent_claims_are_unique(tmp_path):
    import threading

    from fleet.db import Store

    path = str(tmp_path / "c.db")
    s1 = Store(path)
    for i in range(20):
        s1.add_task(str(i), 1.0)
    got, lock = [], threading.Lock()

    def work():
        s = Store(path)
        while t := s.claim_next():
            with lock:
                got.append(t.id)
        s.close()

    ths = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert sorted(got) == list(range(1, 21))
