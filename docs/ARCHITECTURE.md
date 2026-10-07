# Architecture

## Goals and honest limits

Goal: a server you host that runs many Claude agents in parallel on tasks, tells you by
SMS or phone call when something needs you, browses the web and clicks on your behalf,
later reaches your laptop and phone, and holds every task to a review standard.

What it **cannot** do, so nobody plans around it:

- **It cannot drive the claude.ai app, Claude Projects, or your claude.ai chats.** There
  is no supported API for them, and scripting the web UI would break Anthropic's terms and
  your login. The fleet uses the **Anthropic API** (Messages API with tool use) directly.
  Project-like context is rebuilt by putting instructions and files into the task.
- **Orchestrating Claude Code** works by running the `claude` CLI headlessly
  (`claude -p ... --output-format json`) as a tool, or by using the Claude Agent SDK in a
  future worker type. It starts new sessions; it does not attach to a session you have
  open in a terminal or the desktop app. The current wrapper is tested against a fake
  binary only.
- **Review is not proof.** A second model catches many errors, but it shares blind spots
  with the first. High-stakes actions rely on the approval gate, not on review.
- **Browser clicks cannot be perfectly classified.** The gate treats anything that is not a
  plain link as needing approval, which is safe but chatty. It cannot detect that a link
  performs a state change via GET.
- **Cost caps are enforced between API calls**, so a single call can overshoot the cap by
  its own cost. Token prices are a hard-coded table that must be kept current.
- **Phone/laptop control** needs software installed on those devices and OS permissions
  you grant. iOS in particular does not allow arbitrary remote control; see M5.

## Components

```
 owner --CLI/(later web)--> SQLite (tasks, approvals) <--- Worker pool (asyncio, N workers)
   ^  fleet add-task/status/approve                              |
   |                                                             v
   |                                          Agent loop (Messages API, tool use)
   | SMS / call                                   |           |            |
   +---- Alerter (Twilio | Fake | Null)      Approval gate   Tools:     Budget meter
                                              (default deny)  files, browser,
                                                              claude_code_session
                                                    |
                                          Reviewer agent (no tools)
```

| Module | Role |
|---|---|
| `fleet/db.py` | SQLite (WAL) task queue: statuses `queued, running, waiting_approval, done, failed`, attempts/retries, per-task budget and usage, approvals table. Claims are atomic (`BEGIN IMMEDIATE`). |
| `fleet/worker.py` | `run_pool`: N asyncio workers claim tasks, run agent, run review, then mark done / requeue with reviewer feedback / fail. |
| `fleet/agent.py` | Tool-calling loop, adaptive thinking, shared `Budget`, handles refusal and max_tokens stop reasons. |
| `fleet/review.py` | Second agent returns `VERDICT: PASS|FAIL - reason`; anything unparseable is a FAIL. |
| `fleet/gate.py`, `fleet/tools.py` | Every tool must declare `approval=` True, False or a per-call classifier. Classifier errors count as "needs approval". |
| `fleet/browser.py` | Playwright Chromium tools with URL policy. |
| `fleet/alerts.py` | `Alerter` protocol; `TwilioAlerter` (SMS always, plus a voice call when critical), `FakeAlerter`, `NullAlerter`. |
| `fleet/claude_code.py` | Headless Claude Code session as an approval-required tool. |
| `fleet/cli.py` | `add-task`, `run`, `status`, `approve`. |

## Data flow for one task

1. `fleet add-task` inserts a row (`queued`, budget, max attempts).
2. A worker claims it (`running`, attempts+1) and builds a per-task tool registry and a
   fresh browser session.
3. The agent loop calls the model; each `tool_use` goes through the gate. Safe calls run.
   Gated calls create an `approvals` row, set the task to `waiting_approval`, alert the
   owner, and poll. Approved runs, denied or timed-out returns an error result to the
   model.
4. Token usage is priced and persisted after every API call; exceeding the budget fails
   the task with no retry and a critical alert.
5. The reviewer sees the task and the result. PASS marks the task done. FAIL requeues it
   with the feedback appended, until attempts run out; the final failure alerts the owner.
6. On restart, `running`/`waiting_approval` tasks are requeued and open approvals denied.
   This assumes **one runner process** owns the queue.

## Threat model

Assets: your API key and budget, Twilio credentials, any accounts the browser is signed
into, the host filesystem, and your laptop/phone later.

| Threat | Mitigation | Residual risk |
|---|---|---|
| Prompt injection from web pages or files steers the agent | System prompt treats content as data; all destructive/outward actions need human approval; reviewer is a separate context | Injection can still waste budget or leak data the agent can read into its own answer. Do not give agents access to secrets. |
| Agent takes a harmful action | Default deny, explicit `approval=` required per tool, timeout is denial, fail-closed classifiers | The approval text shows raw tool input; an owner who approves blindly defeats it |
| SSRF via browser (localhost, cloud metadata, LAN) | http(s) only, resolved addresses must be global, re-checked after redirects, opt-in override | DNS rebinding and subresource requests are not blocked; add network egress rules on the host |
| Runaway spend | Per-task USD cap, `max_turns`, `max_attempts`, concurrency limit | Overshoot by one call; unknown models priced at the highest known rate |
| Path traversal / symlink escape in file tools | All paths resolved inside the workspace | Claude Code sessions run with the CLI's own permissions inside the workspace |
| Secret leakage | Credentials only from environment; `.env` is git-ignored; none are logged | Task prompts and results are stored in plaintext SQLite |
| Anyone with DB access can approve | Approvals are rows in the DB | Run on a private host; a web UI would need authentication (M2+) |
| Alert channel failure hides a problem | Delivery errors are logged, never raised; Null alerter logs | No second channel yet |

Deployment guidance: dedicated unprivileged user or container, no cloud credentials on the
box, egress firewall, separate browser profile with no logged-in sessions unless you accept
the risk, Chromium sandbox on (the `--no-sandbox` switch is for trusted containers only).

## Roadmap

- **M1 core (done):** queue, retries, budgets, worker pool, review, approval gate, CLI, tests.
- **M2 browser (done, basic):** navigate, read, click, type, screenshot with SSRF policy and
  form-submit gating. Next: persistent profiles, downloads, per-domain allowlists, a
  human-takeover handoff for CAPTCHAs and logins.
- **M3 alerts (done, basic):** Twilio SMS and calls on failures, budget breaches and
  approval requests. Next: reply-by-SMS approvals (webhook, signature verification and
  sender allowlist), escalation, dedup/rate limit, stuck-task watchdog.
- **M4 laptop agent:** a small signed-in daemon on the laptop that polls the server
  over an authenticated outbound connection (no inbound ports), exposing a narrow tool set
  (open app, read file in allowed folders, run an allowlisted command). Every action is
  approval-gated by default, with an audit log and a kill switch. Use the Claude computer
  use tool only inside a VM or dedicated account first.
- **M5 phone:** Android via a companion app or Accessibility/ADB over a trusted link. iOS
  is limited to Shortcuts/notifications; expect approval and notification features, not
  full control. Reuse the M4 protocol.
- **Cross-cutting:** web dashboard with auth, multi-process safe leasing with heartbeats,
  structured logs and metrics, Agent SDK worker for Claude Code, prompt caching tuning.

## Decisions for the owner

Hosting (home server vs small VPS; the box needs Chromium and outbound HTTPS), Twilio
account and number (and whether voice calls are worth the cost), per-task and monthly
budget, default model/effort tradeoff, and which actions you will ever allow without
approval.
