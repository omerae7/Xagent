# Xagent architecture and reliability audit

Audit date: 2026-11-05 (Asia/Tehran). Implementation was inspected at commit `49c09bcb86fecf2254998c37e3217104b51aecb5` in https://github.com/omerae7/Xagent, source branch `ccr-345833d2-sca9ob`.

## Decision

Preserve the existing architecture and repair its execution contracts. Xagent has real tool handlers, persistent PostgreSQL state, Redis coordination, provider adapters, permissions, and a substantial passing regression suite. However, the current success signal does not reliably establish task completion. Recovery and readiness also differ between execution paths. These are material barriers to unattended production operation.

The audit confirms mechanisms that explain several reported symptoms. It does **not** establish which mechanism caused a particular production incident: no production logs, database snapshot, provider billing records, Telegram account, or live desktop deployment were supplied.

No implementation fixes were made.

## Workspace preservation

- Existing checkout: `/workspace/Xagent`; all 219 tracked files from the verified source commit are available.
- Imported the verified branch from the separately downloaded bare repository; switched to new branch `audit/xagent-architecture` at the exact baseline SHA.
- Preserved the original `work` branch and commit `d48f62e48628fb693961b215384059aab39be876`, and created local source branch `ccr-345833d2-sca9ob`.
- Final tracked/untracked Git status was clean. No tracked source, tests, lockfiles, configuration, or history were edited or deleted. No commits, pushes, merges, deployments, environment draft saves, or publication occurred.
- Dependencies and generated frontend build/cache files were added only in ignored locations; the Python virtual environment and audit artifacts are outside the checkout. Test migrations touched disposable databases only, never an existing application database.
- Earlier Git failures were sandbox denials when connecting to the platform proxy. Read-only Git access succeeded outside that sandbox. The initial workspace had selected `main`; it did not contain the application implementation.

## Validation evidence

| Check | Executed result | What it establishes |
|---|---|---|
| Existing Python suite: `pytest -q --junitxml=...` | **222 passed, 0 failed, 0 skipped**, 63.05 seconds | Existing Python assertions pass with real PostgreSQL/pgvector and Redis, but simulated providers and several simulated integrations |
| `ruff check packages services tests` | Passed | Configured lint rules |
| Python `compileall` for packages, services, desktop daemon, tests | Passed | Python compilation |
| `bash -n` on installer, updater, control, entrypoint, desktop, updater test scripts | Passed | Shell syntax; no scripts deployed or applied |
| CI executable-bit checks | Passed | Four required updater/control/test entrypoints are executable |
| `npm ci --no-audit --no-fund` | Passed | Locked frontend dependency installation; no vulnerability audit was run |
| `npm run typecheck` | Passed | TypeScript check |
| `npm run build` | Passed | Vite production bundle, 2,129 modules transformed |
| Migration `upgrade head`, isolated probe DB | Passed through **0007** | Fresh PostgreSQL schema initialization; not proof of every populated upgrade/rollback |
| Local real HTTP login, health, dashboard | **200 / 200 / 200** before saturation | API bootstrap, authentication, database/Redis health, dashboard queries |
| Eight concurrent SSE streams under `lite` | All opened; **8 DB connections checked out; health timed out after 1 second** | Reproduced resource retention and resulting request blockage |
| Compose validation | Passed with an external copy pointing `env_file` to tracked `.env.example` and dummy test values | Example topology parses. Original invocation correctly failed because no local `.env` exists |
| Complete application image build, full desktop stack, updater shell end-to-end | **Not run** | No deployment or container-level runtime correctness claim |
| Live paid model APIs, real Telegram/voice/browser account workflows, external Cursor jobs | **Not run** | No credentials or external side effects were requested or exercised |

Runtime: Python 3.12.14; Node 24.22.1 and npm 11.9.0; CI specifies Node 22. Frontend success on Node 24 is not Node 22 verification. Python dependency declarations are mostly ranges and have no Python lockfile; exact audit resolutions are in `python-dependencies.txt`. The audit is reproducible against those recorded versions rather than an assumed historic deployment environment.

Artifacts: `pytest.log`, `pytest.xml`, `ruff.txt`, `probe.py`, `probe.log`, `probe-migration.log`, `sdk_probe.py`, `sdk-probe-confirmed.log`, `python-dependencies.txt`, `code-index.txt`, and external `compose.audit.yaml`. The early `sdk-probe.log` is an unsuccessful diagnostic attempt, not the confirmed result; the corrected SDK probe uses the SDK's required httpx2 mock transport without inherited proxy mounts. An obsolete stalled diagnostic process was terminated. Both disposable PostgreSQL/Redis containers were stopped after validation and retained without deleting their data.

## Actual execution architecture

An authenticated request creates a persisted Run and enqueues its ID in Redis. Worker tasks consume the stream, acquire a Redis execution lease, and checkpoint the pipeline into `Run.state`. Classification selects a path; complex tasks add goal/planning model calls. Execution selects provider/model chains, retrieves scoped memory, renders tool schemas and capability information, calls the model, evaluates permissions, invokes actual handlers, records tool results, and evaluates a final answer. Finalization commits the Run, then worker postprocessing settles routine/task counters and status.

The manager is the same execution engine with model/role selection, not an independent reliable orchestrator. `employees.delegate` creates a child Run and calls the runner synchronously, with depth limited to one, a separate conversation, and cost/step caps. It is real delegation, but the parent remains occupied while waiting. Browser control uses Playwright/CDP and per-run tabs; desktop control uses the shared desktop daemon. Telegram uses Telethon plus persisted inbox/outbox records. These are implemented integrations, not merely README claims; their live operational readiness is a separate question.

## Confirmed defects and their causes

### A1 — P1: final success does not require successful execution evidence

**References:** `packages/agent_core/xagent_agent/pipeline.py`: `_validate` at 1088, simple branch at 1095, malformed evaluator fallback at 1111, recovery exit at 662, `_execute` at 303, `_finalize` at 1200. `learning.py`: `learn_from_run` at 37, success derivation and `record_experience` below it. `packages/tools/xagent_tools/tasks_exec.py`: `settle_task` at 71.

**Reproduction:** `probe.py` passes failed `telegram_send` evidence (`ok=False`, `verified=False`) and a nonempty completion claim into the real `_validate(..., complex_task=False)`. Result: `passed=True`. Injecting a malformed evaluator response into the complex branch also returns `passed=True`. No Telegram message is sent by this reproduction; it proves acceptance behavior, not a live Telegram outage.

**Cause:** simple validation checks only that text exists. The complex path defaults invalid JSON to success. After one recovery, the execution loop breaks even when the second verdict fails; `_execute` then finalizes as succeeded. A model that produces prose without tool calls can satisfy this control flow.

**Impact:** dashboard/task/routine success can mean that text was produced rather than work completed. `learn_from_run` can further label a failed routine tool path as `verified_success` when validation passed, contaminating retrieved experience. Final success, reporting, and learning currently share an insufficient trust signal.

**Recommendation:** introduce typed execution outcomes (completed, partial, failed, awaiting input, unknown external result); validate task-specific postconditions and required effects. Treat malformed/unavailable evaluation as unknown, not proof of success. Preserve ordinary conversational answers without requiring meaningless tool calls. Do not promote experience to verified success without execution evidence.

**Compatibility/acceptance:** keep API status aliases during transition; add an explicit outcome/verification field if necessary. A prose-only completion claim for an action request and a failed tool must never mark the task done. Second failed verification must terminate with an honest non-success outcome. Existing chat-only greetings must still work.

### A2 — P1: event routines bypass the shared routine lifecycle

**References:** `services/worker/xagent_worker/inbox.py`: `process_event` at 55, routine selection/creation at 82 onward. Compare `scheduler.py` `_dispatch_due` at 55 and `packages/tools/xagent_tools/lifecycle.py` `claim_occurrence` at 65. `scheduler.py` `sweep_lifecycle` at 121 excludes event routines.

**Reproduction:** create an active event routine with `max_runs=1, run_count=1`, exhausted lifetime budget, and `end_at` in the past; deliver a matching persisted event. The real handler creates another Run and increments `run_count` to 2. Separately, an expired active event routine remains `active` after the real lifecycle sweep.

**Cause:** event dispatch directly constructs Run instead of using the shared mission validity, remaining-budget, occurrence claim, and active-run checks. It does not copy mission identity into Run state. Its event lease is temporary, not a persisted occurrence key. Event routines are excluded from timed expiry sweep; selection also excludes open circuits without implementing the scheduled cooldown-to-half-open recovery path.

**Impact:** expiry/run/budget controls differ by trigger; an event routine can stay active after expiry or permanently stop dispatching after its circuit opens. Mission enforcement in the executor cannot substitute for a missing mission identity. The probe proves limit bypass and stale active status; no sensitive operation or permission bypass was executed.

**Recommendation:** use one event/schedule dispatch contract with durable occurrence keys, lifetime/mission constraints, concurrency policy, and circuit recovery. Preserve existing filters and trigger semantics.

**Acceptance:** expired, revoked, exhausted, and pending grants never dispatch; concurrent duplicate events create at most one Run; allowed future events resume after cooldown; expired event routines close even if no more events arrive. Review existing event routines and grants before rollout. A new persistent event occurrence key/index may require an additive migration and user approval.

### A3 — P1: completed runs can escape task/routine settlement after a crash

**References:** `pipeline.py` `_finalize` at 1200; `services/worker/xagent_worker/main.py` `_execute` at 72; `scheduler.py` `after_routine_run` at 148, `after_task_run` at 290, `reconcile_runs` at 302, `sweep_lifecycle` at 121.

**Reproduction:** persist a successful Run with cost, representing a crash after finalization but before postprocessing; run reconciliation and lifecycle sweep. The probe's retained active interval routine still has `success_count=0` and `spent_usd=0`. Its deliberately future `next_run_at` also leaves it active despite `max_runs=1`; that inconsistent persisted schedule is a recovery robustness case, not a claim that normal occurrence creation produces that combination. An earlier one-shot variant closed the routine but still lost success/cost settlement.

**Cause:** finalization and settlement are separate commits. Reconciliation scans only queued/running Runs; no sweep replays terminal Runs with missing `routine_settled`/`task_settled` markers. Worker postprocessing exceptions are logged and then the stream job is acknowledged. Lifecycle closure is not equivalent to cost/outcome settlement.

**Impact:** counters, budgets, reports, Task state, and Routine state can disagree with committed Run state. The event expiry defect A2 is an additional, independently proven reason for completed-window routines remaining active.

**Recommendation:** persist durable settlement work in the finalization transaction, or repeatedly reconcile terminal unsettled Runs using existing idempotent settlement functions. Expand lifecycle selection to detect all exhausted conditions, not just selected schedule fields.

**Acceptance:** fault injection after every finalization/postprocessing boundary must eventually yield exactly-once counters and cost deltas, correct task completion, no stale exhausted routines, and one report. Reuse existing settlement markers; a durable outbox table would require a separately approved migration.

### A4 — P1: inbox failures are acknowledged and lose their retry path

**References:** `main.py` `consume_inbox` at 129, exception and unconditional `xack` at 137–140; `telegram.py` `_store` at 130; `inbox.py` owner command commit at 66–73. Compare Run consumer's `xautoclaim` at `main.py:97`.

**Reproduction:** enqueue a real audit inbox event in isolated Redis, inject a processing exception into `consume_inbox`. Result: database event remains `new`, Redis pending count is **0**. It is not available to `XREADGROUP ... '>'` again.

**Cause:** an exception still leads to ACK; inbox consumption has no reclaim path comparable to Run consumption. A second loss window exists between committing a Telegram inbox row and `enqueue_inbox`. Owner commands are marked routed before the handler succeeds. These adjacent windows are source-confirmed, not separately crash-injected.

**Recommendation:** acknowledge only completed or durably retry/dead-lettered events; reconcile committed new inbox rows and reclaim stale pending work; persist processing attempts and owner command dispatch atomically.

**Acceptance:** injected database, Redis enqueue, handler, and process-crash failures result in recovery or a visible dead letter, never silent abandonment. Preserve external-event deduplication. Any retry-state schema additions need approval.

### A5 — P1: SSE retains database sessions and can block the API

**References:** `services/api/xagent_api/routers/live.py` `stream` at 56; `deps.py` `get_session` at 23 and `current_user` at 58; `packages/core/xagent_core/db.py` `get_engine` at 56; `config.py` `db_pool_size` at 82.

**Reproduction:** start the real API locally under `lite`, authenticate, request health/dashboard successfully, then keep eight `/api/events/stream` responses open. Pool checkout goes from 0 to 8 (base 4 + overflow 4). A health request that previously returned 200 then times out at 1 second. Close the audit streams and shut down that audit server.

**Cause:** stream authentication queries through a yield-scoped request session which stays alive for the streaming response. Each long-lived stream retains a transaction/connection. Standard profile permits 20 total connections per engine; this is a profile-dependent exhaustion threshold, not a measured production threshold.

**Recommendation:** finish authentication in a short database scope before returning the stream; do not retain ORM session state during SSE. Implement reconnect resync because Redis Pub/Sub and the bounded in-process queue do not replay missed events. Separate liveness from dependency readiness and bound health queries.

**Acceptance:** dozens of open streams do not cause corresponding DB checkout growth; health/dashboard remain responsive. Test logout/token revocation behavior on long-lived streams. No schema migration should be needed for the session lifetime correction.

### A6 — P1: Anthropic retries occur below the per-attempt ledger

**References:** `packages/agent_core/xagent_agent/providers/anthropic_provider.py` constructor at 54 sets SDK `max_retries=2`; `llm.py` `call_llm` at 35 reserves one ledger entry before each `provider.chat`, and also retries up to three times. `usage_ledger.py` `begin_attempt` at 52.

**Reproduction:** SDK transport probe injects HTTP 500 using a local mock transport. One adapter `chat` invocation produces **three underlying HTTP attempts**. The corrected result is in `sdk-probe-confirmed.log`. This proves hidden attempt multiplication, not that every failed request incurred provider charges.

**Impact:** a full outer chain can make up to nine network attempts per Anthropic model for retryable failures, while recording three top-level reservations. Actual billable timeout outcomes and latency can exceed the intended accounting granularity.

**Recommendation/acceptance:** assign retry ownership to one layer, or instrument every SDK wire attempt with a durable reservation. A three-attempt policy must emit exactly three transport requests and three reconciliable records, including timeout/unknown outcomes. Keep TLS and SDK protocol compatibility; no database migration is inherently required.

### A7 — P2: capability readiness is partly inferred from configuration rather than tested

**References:** `manifest.py` `build_manifest` at 40; browser branch at 82; model and installed-tool branches below it. `pipeline.py` `_self_knowledge` at 769; `_tools_for` at 706; `registry.py` `installed_tools` at 107.

**Reproduction:** call the real manifest builder with `sandbox_kind='desktop'` while no desktop/CDP service exists. It reports browser/desktop as ready. `runner` receives the same ready classification even though the runner image has no Chromium/XFCE. The probe uses actual application/database code, not a live desktop.

**Cause:** sandbox string is treated as readiness; enabled model capability flags are not equivalent to a healthy enabled provider; installed-tool counts include disabled tools, while actual discovery filters them. `_self_knowledge` also lists admin/host groups in general instructions even when the current principal cannot load them. Catch-all manifest failure handling can silently remove the status context.

**Recommendation:** retain the manifest renderer but feed it capability observations with timestamp, health, setup requirements, principal restrictions, and actual executable tool IDs. Cache bounded health probes; do not add network health requests to every model turn. Filter presentation and execution from the same policy.

**Acceptance:** unavailable desktop/runner-only deployment, disabled provider, disabled installed tool, employee, and external principal all receive accurate capability states. Unknown is distinct from ready. No schema change is necessary unless observations are persisted.

### A8 — P2: working-memory and execution transcript budgets are incomplete

**References:** `packages/memory/xagent_memory/working.py` `load_window` at 92; `context.py` `ContextBudget` at 38 and `build_context` at 88; `pipeline.py` transcript creation at 580, execution request at 644, full tool-output append at 917; `llm.py:35`.

**Reproduction:** store one 40,000-character message and call `load_window(..., budget_tokens=100)`. It returns approximately **10,001 estimated tokens**. The first message is accepted even when over budget. Source inspection also shows every loop resending the entire growing transcript, with no final input-plus-output fit check against the selected/fallback model's context window.

**Impact:** oversized requests, repeated context-limit failures, and cumulative token consumption. If each step adds similar-sized output, cumulative transcript input grows approximately quadratically with step count. Monetary/token safety reservations exist, but are not a context-fitting strategy.

**Recommendation:** apply a unified request budget including system instructions, schemas, history, raw provider replay items, images, output reserve, and fallback context windows. Summarize/store large observations with retrievable references while preserving valid tool-call/result pairing and provider reasoning requirements. Fix first-message overflow explicitly.

**Acceptance:** adversarial long messages and multi-step large tool outputs stay within measured request limits. Compare complete-task cost and verified success against baseline; do not optimize by silently losing constraints or evidence. No mandatory migration for request budgeting.

## Additional architecture risks established by source inspection

These warrant tests and design changes but were not reproduced as production incidents:

1. **Scheduler head-of-line delay:** `main.py:scheduler_loop` at 212 awaits `run_dream` inline at 245. Default Dream deadline is 300 seconds, configurable to 1,800; while waiting, routine ticks, task ticks, polling, reconciliation, and pending Telegram flushing on that coroutine wait too. Move long work into separately supervised jobs with bounded capacity. Acceptance: a deliberately slow Dream does not delay due dispatch beyond a defined scheduler SLO.
2. **Lease loss lacks fencing:** `pipeline.py:277` heartbeat ignores a false `extend_lease` result; heartbeat exceptions can stop renewal, while execution can continue. There is no monotonic fencing token checked by each side-effect handler. A checkpoint after an external effect cannot prevent replay if the process dies before recording it. Telegram has an outbox; generic MCP/shell/browser effects do not have equivalent reconciliation. Require cancellation/fencing on ownership loss and operation-specific idempotency. Do not claim universal exactly-once external execution.
3. **Unbounded consumer tasks:** `main.py:_dispatch` creates tasks before capacity is acquired inside `_execute`; the stream can be drained into many waiting tasks. Reconciliation can enqueue already-waiting queued Runs. Bound intake/pending work and expose backlog age, rather than relying solely on a semaphore around execution.
4. **Control job durability:** control messages are ACKed before `_control` finishes and run in untracked tasks (`main.py:147–187`). A worker crash can discard an accepted install/update/manual Dream command. Reuse a durable job lifecycle; do not treat successful enqueue as completed work.
5. **Employee capability limits are ambiguous:** configured `Employee.tool_groups` seed tools, but dynamic `tools_load` can add other non-admin/non-host groups. If roles are intended as a permission boundary, enforce an explicit allowed capability set server-side. If they are only preferences, label them accordingly. Global Harness still applies; this is not proof of admin access.
6. **Shared desktop is a shared authority surface:** per-run browser tabs are helpful, but share cookies/browser context and one visual desktop. Desktop mouse/keyboard actions and local commands can affect other work or logged-in accounts. Add an explicit desktop lease and enforce effect permissions at the bridge; separate authenticated browsing from untrusted command execution. Live tests must verify takeovers, redirects, concurrent jobs, and unknown form effects.
7. **Worker health does not prove consumers are alive:** metrics heartbeat can continue while a consumer/scheduler/settings coroutine has failed. `Worker.run` starts tasks without supervising task failures; Compose has no worker healthcheck, and updater health uses heartbeat age. Track each essential loop's freshness/progress, surface fatal errors, and restart/recover intentionally.
8. **Frontend stale state after missed events:** SSE uses Pub/Sub without durable event IDs; queue-full events are discarded. `apps/web/src/lib/live.ts` listens to a fixed list, omitting events such as `employee.status`, `model.health`, and `model.fallback`. Add reconnect invalidation/resnapshot and explicit subscription coverage where relevant. Passing a build does not verify page behavior or real browser sessions.
9. **Model routing observes transport success more than task quality:** `router.py:_stats` uses `UsageEvent.success`, so successful API responses can look reliable even when tasks fail verification. Static quality priors are heuristics. Keep provider health and empirically verified task success separate; optimize cost per completed task instead of per HTTP success.
10. **Deployment reproducibility and recovery remain unproven:** Python ranges and mutable image tags allow drift. Updater code contains useful locks, backups, temporary schema validation, health checks, and rollback handling, but this audit did not execute its shell end-to-end harness or test rollback with production-like data. API startup performs migrations; multi-replica startup needs migration serialization. Startup migrations and updater restoration must never be tested against user data during an audit.

## Components to retain and build upon

- The existing typed provider-neutral request/message/tool interface, provider-specific reasoning replay, and adapters for Responses, compatible chat, Gemini, and Anthropic. Contract simulations passed; retain them and add wire/live tests.
- Durable usage-attempt ledger with independent commits, advisory-lock preflight reservations, unknown-outcome reservations, provider-reported token subsets, and separate Dream limits. Correct the hidden retry boundary rather than replacing the ledger.
- Shared scheduled occurrence claims, unique `Run.dedupe_key`, PostgreSQL row locks with `SKIP LOCKED`, schedule window arithmetic, no-change prechecks, circuit breaker state, and idempotent settlement markers. Reuse these for event routines and reconciliation.
- Server-side Harness decisions and owner/admin separation; sensitive-category restrictions, approval checkpoints, scoped mission checks, external-event trust marking, and Telegram outbox uncertainty handling. They are useful foundations; prose instructions are not substitutes for these controls.
- Scoped semantic/episode/experience queries, Persian normalization, lexical/vector rank fusion, embedding-model separation, revision snapshots and undo/probation machinery. Hash embeddings are a deterministic low-cost baseline, not evidence of high semantic recall. Evaluate retrieval quality on representative Persian/English tasks before changing models.
- Browser per-run tab ownership and Takeover checks; runner separation from backend secrets; actual daemon/MCP transport implementations. Validate the remaining shared-session boundaries.
- Authentication with password hashing, httpOnly/SameSite cookies, CSRF header checks for cookie writes, role dependencies, token-version invalidation, and serializers that hide selected secret columns. SSE session lifetime/revocation needs specific correction and tests.
- Existing updater backup/rollback structure and extensive test suite. Extend targeted regression coverage instead of a rewrite.

## Why the current suite can pass despite these defects

`tests/conftest.py` explicitly provides a scripted model; vendor tests use `fake_vendors.py`. Browser benchmarks replace pages with `FakePage`; routine Telegram benchmarks use `FakeTelegram`. Several benchmarks assert Run status equals succeeded rather than asserting an independently observable effect. With A1, that status is insufficient evidence. Database and Redis are real in this audit, and some local bridge/network tests exercise real transports/subprocesses, so the suite is not uniformly mocked. Nevertheless, it does not prove that live providers understand the manifest, real Telegram sessions recover, Chromium/XFCE works, or the deployed worker/service topology remains available.

Retain the fast suite. Add deterministic invariant/fault tests, real subprocess worker tests, disposable container integration tests, and a separately authorized, bounded live acceptance tier. Do not make paid API access or external sends a hidden prerequisite for every unit test.

## Prioritized implementation plan and acceptance gates

| Phase | Targeted scope | Acceptance gate | Dependencies and compatibility |
|---|---|---|---|
| 1 — Truthful execution | A1; outcome propagation; learning gate; strict evaluator parsing | No failed/no-tool action becomes completed; honest partial/unknown results; existing conversational behavior preserved | Agree task postconditions and API/UI outcome mapping; additive fields preferred |
| 2 — Dispatch and recovery | A2–A4; terminal settlement retry; inbox/control durability; fenced execution | Faults at commit/enqueue/effect/settlement boundaries recover without lost work or duplicate confirmed effects; expired grants never dispatch | Reuse lifecycle helpers. Durable outbox/index changes require explicit migration approval; backfill from existing rows with dry-run counts |
| 3 — Availability | A5; independent Dream jobs; bounded intake; essential-loop supervision; health deadlines | SSE load does not consume DB capacity; slow Dream does not delay scheduler; worker failures are visible and recoverable | No schema change expected for SSE/supervision; define concurrency and latency SLOs before load tests |
| 4 — Capability and cost correctness | A6–A8; role capability policy; full context-fit builder; task-quality routing metrics | Every wire attempt accounted; unavailable integrations never advertised ready; request limits respected; task cost compared at equal verified completion | Preserve provider replay formats and existing pricing semantics; version stored observation formats if introduced |
| 5 — Live release qualification | Full disposable stack, migrations on copied fixtures, updater rollback, real browser/Telegram/model scenarios | Independent evidence for each action, recovery and bounded spend; frontend authenticated flows verified; restore drill succeeds | Explicit test credentials/recipient targets and limits; isolated nonproduction data; no production deployment or automatic merge |

Before implementation, select the first phase and approve any schema/compatibility changes separately. No functioning component needs replacement to begin. Any data repair should first produce a read-only inventory of inconsistent routines, unsettled terminal Runs, new inbox rows lacking dispatch, unknown external actions, and potentially misclassified experiences. Preserve evidence and history; never blindly replay uncertain external sends or retroactively relabel historical work as verified.

## Current boundary

The requested audit and baseline validation are complete. There is a verified working development branch and passing baseline, plus reproduced reliability defects. There is no claim of production readiness, live-provider qualification, complete Docker/updater validation, or production incident attribution. Implementation remains unstarted.
