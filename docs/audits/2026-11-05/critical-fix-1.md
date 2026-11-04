# Critical Fix 1: evidence-backed completion

Repository: `omerae7/Xagent`; baseline `49c09bcb86fecf2254998c37e3217104b51aecb5`; working branch `audit/xagent-architecture`.

Only the false-success defect is addressed. No scheduler redesign, provider retry fix, SSE fix, production configuration, schema migration, deployment, push or merge is included. The original architecture report and sanitized baseline reproductions remain alongside this report. Original unredacted audit artifacts remain in the working environment outside the repository; they have not been added to Git.

## Root causes and changes

1. **Simple validation accepted any nonempty answer.** `pipeline.AgentRunner._validate` now requires well-formed explicit success criteria and usable execution evidence for executable work. `validation.evidence_issues` checks result provenance, concrete planned tool hints and missing call results. `tools_load` is discovery, not proof of execution. Ordinary greetings continue to work without a tool.
2. **Failed, denied or missing tool results were not consistently represented in evidence.** `_process_calls` and `_mission_gate` now record failed/blocked attempts with call ID and an operation signature. Handler results are checked before transcript/checkpoint publication: absent results, malformed flags/types, empty results and explicitly failed verification cannot become success evidence. Action effects require the existing `ToolResult.verified=True` contract. The public Tool/ToolResult interface is unchanged.
3. **Malformed evaluation output defaulted to success, and truthy strings could count as booleans.** `validation.evaluation_verdict` requires a literal JSON boolean, validates response shape, and requires a successful evidence reference for every indexed criterion of executable work. Missing/duplicate criteria, unknown evidence references, unverified-effect references and unavailable evaluators fail closed. Failed evaluator verdicts remain failed.
4. **The loop returned an answer after exhausting one recovery even if evaluation failed again.** It now raises `ValidationFailed`; `_execute` finalizes a failed Run with `stop_reason=validation`. The rejected completion prose is not posted as a final success message. The failure message explicitly warns that partial effects may already have happened and must not be blindly repeated.
5. **An early return could bypass validation.** `_execute` now guards the final success transition. Only the existing deterministic command and observed no-change watch paths are exempt. Goal clarification pauses the Run with `stop_reason=clarification`; existing settlement leaves its Task waiting rather than done.

## Evidence and recovery semantics

Evidence records now include `call_id`, operation `signature`, and `requires_verification` in existing JSON state. This is additive metadata; no database migration is required. A verified retry of the same operation may resolve that operation's earlier failure, while retaining the failed attempt in history. An unrelated verified action cannot hide a failed external effect.

Read-only observations are distinct from external effects. A failed read may use a subsequent successful observation through the existing fallback path, but must then pass the structured criterion evaluator. The failed read is never relabeled successful. Ordinary browser navigation/interactions provide observations, not proof of a send, purchase or other external effect. Dynamically escalated categories still require explicit effect verification. Live browser behavior is not qualified by this change.

Deterministic watch prechecks that actually collected events now register their observations as evidence. This preserves the existing light routine summary path and its no-change optimization; it does not expand mission permissions. No fabricated tool result is created when an actual handler failed.

The existing task/routine settlement functions are unchanged. They consume the corrected Run status: failed validation produces a failed Task and failure accounting; verified completion produces a done Task; clarification produces a waiting Task. Learning receives the failed status rather than a falsely successful episode. Historical data is not rewritten.

## Regression coverage

`tests/test_execution_validation.py` exercises the production runner, finalization and worker postprocessing with real PostgreSQL/Redis and filesystem operations. Model responses are deliberately scripted so each failure can be reproduced. It covers:

- Failed tools, explicit verification failure, unknown effects, missing/empty/malformed ToolResult values and invalid flag types.
- Action claims with no calls, discovery-only calls and unknown tools.
- A real filesystem path-policy refusal without mocking the tool.
- Real successful file creation and content readback, with successful Run and Task settlement.
- Invalid JSON, missing fields, string/numeric/null booleans, malformed issues, absent criterion coverage, nonexistent evidence references, empty evidence references and repeated negative evaluation.
- Unavailable evaluator and provider transport error.
- Empty final answers, unvalidated early pipeline returns, and clarification leaving a Task waiting.
- Routine failure counters, failed learning episode status, missing planned-step evidence, verified retry resolution, observational-interface compatibility, and greetings without unnecessary tools.

The initial 17-case regression run against the unchanged pipeline produced **15 failures and 2 passes**, reproducing the defects. Subsequent tests expanded that coverage. Exact final counts and command outcomes are recorded in `verification.json`; raw intermediate/final logs and JUnit XML remain at `/workspace/xagent-audit-results`.

Existing tests were updated only where necessary for the new contract:

- `tests/conftest.py`: scripted evaluator supplies criterion/evidence references; its intentional loop scenario matches the original test request rather than accidentally matching server recovery notes.
- `tests/test_e2e.py`, `tests/test_manager.py`: disconnected/forbidden sends and rejected security changes must fail instead of passing because a message was produced.
- `tests/test_general_acceptance.py`: scripted employee sales figures with no retrieval evidence must be rejected. The separate verified employee delegation tests remain in the suite.
- `tests/test_autonomy_acceptance.py`: the deliberately naive model's rejected/failed actions are now counted as failures. The existing three-failure circuit opens, and the failed second send stays failed in the outbox. The test retains occurrence deduplication, concurrent scheduler checks, recovery, blocked-sender checks, expiry, report idempotence and accounting assertions. It no longer claims ten successful executions when actions actually failed.
- `tests/test_usage_dream_guard.py`: a test-only checkpoint bounds this post-response crash test to its own three episodes. Without isolation, additional regression episodes exhaust the real Dream preflight token limit before the intended fault injection. No production limit was relaxed and no Dream implementation changed.

## Validation interpretation

Passing scripted model tests proves server-side control flow and invariants, not autonomous competence against real model APIs. The filesystem success and refusal, database persistence/settlement, Redis coordination and actual API handlers used in tests are real local integrations. No external Telegram send, paid model call, authenticated browser workflow or production action was performed.

No frontend code changed; its earlier audit build/type-check results are preserved as baseline evidence, not represented as newly rerun checks. Current Python validation includes the full suite, lint, compilation and whitespace checks; see `verification.json`.

## Compatibility and remaining risks

- Status names and tool interfaces are preserved. Some results intentionally change from `succeeded` to `failed`, or from premature completion to `paused`/waiting. This is the requested correctness change, not a display-only adjustment.
- Complex evaluations now require indexed criteria and valid evidence IDs. A provider that ignores the schema fails safely. This stronger prompt uses a 1,200-token output allowance instead of 600; actual usage is still metered by the existing ledger and limits. Failed or recovered read paths may need an extra evaluation/recovery call. No token-efficiency improvement is claimed.
- Action detection is conservative and uses existing routing/planning metadata plus completion-claim detection; it is not a general natural-language proof system. Some nontrivial textual assignments or old checkpoints lacking provenance may be refused. Review such cases explicitly rather than silently assuming they were verified.
- `verified=True` is a handler's verification contract. This fix validates its use and provenance; it does not independently audit every handler's verifier. Ordinary browser interactions remain observations. Effect classification, semantic relevance of model-selected evidence, and third-party API truthfulness still require live acceptance tests.
- Fail-closed results do not roll back prior side effects. Unknown effects are not evidence of completion and must not trigger blind replay. Existing Telegram outbox protections remain intact. General external-effect reconciliation and lease fencing remain separate audit findings.
- Correct failure propagation can open the existing routine circuit breaker sooner than the prior falsely successful behavior. A schedule reaching its end can still have `completed` lifecycle status with nonzero failed execution counts; the existing lifecycle semantics were not redesigned here. Operators must distinguish schedule expiry from successful work.
- Settlement durability after a crash, inbox dispatch durability, capability readiness, SSE session retention and hidden provider retries remain open audit findings. No production-readiness claim is made.

## Review boundary

All changes are prepared locally on the isolated branch for review. No commit, push, merge, deployment, production data operation, environment configuration save or publication was performed. Stop after this fix; do not begin another audit item without further instructions.
