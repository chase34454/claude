"""Command line: fleet add-task | run | status | approve."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Config
from .db import PENDING, Store


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-task", help="queue a task")
    a.add_argument("prompt")
    a.add_argument("--budget", type=float, help="USD cap (default FLEET_DEFAULT_BUDGET_USD)")
    a.add_argument("--max-attempts", type=int, default=2)
    r = sub.add_parser("run", help="run the worker pool")
    r.add_argument("--concurrency", type=int)
    r.add_argument("--once", action="store_true", help="exit when the queue is empty")
    s = sub.add_parser("status", help="show tasks and pending approvals")
    s.add_argument("task_id", nargs="?", type=int)
    ap = sub.add_parser("approve", help="approve or deny a pending request")
    ap.add_argument("approval_id", type=int)
    ap.add_argument("--deny", action="store_true")
    ap.add_argument("--reason")
    return p


def main(argv: list[str] | None = None, *, client=None, alerter=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env()
    store = Store(cfg.db_path)
    try:
        return _dispatch(args, cfg, store, client, alerter)
    finally:
        store.close()


def _dispatch(args, cfg: Config, store: Store, client, alerter) -> int:
    if args.cmd == "add-task":
        tid = store.add_task(args.prompt, args.budget or cfg.default_budget_usd, args.max_attempts)
        print(f"queued task {tid}")
    elif args.cmd == "status":
        if args.task_id:
            t = store.get_task(args.task_id)
            if not t:
                print(f"no such task {args.task_id}", file=sys.stderr)
                return 1
            print(
                f"#{t.id} {t.status} attempts={t.attempts}/{t.max_attempts} "
                f"cost=${t.cost_usd:.4f}/${t.budget_usd:.2f}"
            )
            for label, val in (
                ("result", t.result),
                ("review", t.review_notes),
                ("error", t.error),
            ):
                if val:
                    print(f"{label}: {val}")
        else:
            for t in store.list_tasks():
                print(
                    f"#{t.id:<4} {t.status:<17} ${t.cost_usd:>7.4f}/${t.budget_usd:<6.2f} "
                    f"{t.prompt[:60]!r}"
                )
        for ap in store.list_approvals(PENDING):
            print(f"PENDING approval #{ap.id} task={ap.task_id} {ap.tool} {ap.tool_input}")
    elif args.cmd == "approve":
        if not store.decide_approval(args.approval_id, not args.deny, args.reason):
            print("approval not found or already decided", file=sys.stderr)
            return 1
        print(("denied" if args.deny else "approved") + f" #{args.approval_id}")
    elif args.cmd == "run":
        from .alerts import alerter_from_env
        from .worker import run_pool

        logging.basicConfig(level=logging.INFO)
        if args.concurrency:
            from dataclasses import replace

            cfg = replace(cfg, concurrency=args.concurrency)
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic()
        asyncio.run(run_pool(store, client, alerter or alerter_from_env(), cfg, drain=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
