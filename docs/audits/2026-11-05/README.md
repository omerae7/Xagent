# Preserved baseline audit

Baseline: `49c09bcb86fecf2254998c37e3217104b51aecb5`.

The architecture report is preserved in full. Baseline reproduction results omit generated record identities; probe source requires externally supplied disposable-test configuration and contains no credential values. Raw runtime logs, database files, local configuration, and authentication data remain outside version control at `/workspace/xagent-audit-results`.

`baseline_probe.py` records the original defects and is historical evidence, not a passing acceptance test after fixes. Run it only on disposable PostgreSQL/Redis instances: it writes test records. Its unrelated audit probes deliberately exercise baseline failures. Do not run it against live data.

The versioned regression suite for Critical Fix 1 is in `tests/test_execution_validation.py`. Verification results and changed-file inventory will be recorded alongside this report.
