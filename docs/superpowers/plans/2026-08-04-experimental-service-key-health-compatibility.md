# Experimental Service-Key Health Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the experimental New API route to the session-token Aurora canary from being falsely classified as an invalid JWT while preserving the existing Schema v1 and legacy JWT behavior.

**Architecture:** The Aurora producer continues to emit Schema v1. Its `database` check gains one additive PASS code, `database_and_service_key_valid`, selected only when channel 1 uses the exact experimental canary base URL and its key matches the strict `.env.canary` service key in memory. The Studio OS workflow validator accepts the matching fixed boolean detail shape while continuing to reject unknown fields, mismatched code/detail combinations, secrets, and all existing invalid inputs.

**Tech Stack:** Python 3.12 standard library, `unittest`, n8n workflow JSON, Node.js test harness.

## Global Constraints

- Do not output or persist the channel key, service key, Token, hash, response body, or Credential identifiers.
- Keep top-level `schema_version=1`; this is experimental backward-compatible support, not the formal production Schema v2.
- Preserve legacy JWT PASS/WARN/FAIL behavior and the existing `refresh_log` contract.
- Modify only the four implementation/test files listed below plus this plan.
- Do not stage, commit, push, create a Git bundle, modify FNOS files, run the producer on FNOS, import/reload n8n, or change SMTP/cron/containers during local implementation.

---

### Task 1: Aurora producer RED tests

**Files:**
- Modify: `tests/test_write_n8n_health_status.py`

**Interfaces:**
- Consumes: `check_database(root: Path, channel_id: int, now_epoch: int) -> CheckResult` and `build_document(...)`.
- Produces: failing tests for the new service-key mode and strict code/detail validation.

- [x] Add a fixture database whose active channel has `base_url=http://aurora-session-renewal-canary:8080` and a non-JWT key, plus a strict `.env.canary` containing the same value.
- [x] Assert `check_database()` returns `PASS / database_and_service_key_valid` with exactly `integrity_ok`, `channel_active`, `channel_base_matches`, and `service_key_matches`, all true.
- [x] Assert a key mismatch returns sanitized `FAIL / database_invalid`, contains no key values, and invalid service-key detail shapes are rejected by `build_document()`.
- [x] Run `python -m unittest tests.test_write_n8n_health_status -v`; expected RED because the code and detail shape are not yet supported.

### Task 2: Aurora producer minimal implementation

**Files:**
- Modify: `scripts/write_n8n_health_status.py`
- Test: `tests/test_write_n8n_health_status.py`

**Interfaces:**
- Consumes: strict `read_env_value(Path, "AURORA_CANARY_AUTHORIZATION")` from `aurora_session_renewal_probe.py`.
- Produces: `database_and_service_key_valid` and its fixed boolean detail shape without changing legacy JWT behavior.

- [x] Add the exact experimental base URL constant and approved PASS code/status mapping.
- [x] Query `key` and `base_url` read-only. For the exact experimental base URL only, compare the database key with the strict env value in memory and emit only four booleans.
- [x] Change detail validation to receive the result code so JWT and service-key shapes cannot be interchanged.
- [x] Keep `database_invalid` sanitized and allow only the matching approved failure shape.
- [x] Run `python -m unittest tests.test_write_n8n_health_status -v`; expected GREEN.

### Task 3: Studio OS workflow RED then GREEN

**Files:**
- Modify: `F:/70_Infrastructure_and_Operations/Solis_Studio_OS/tests/workflows/test_aurora_gateway_health_alert_export.py`
- Modify: `F:/70_Infrastructure_and_Operations/Solis_Studio_OS/workflows/aurora-gateway-health-alert.workflow.json`

**Interfaces:**
- Consumes: Schema v1 `database` check with code `database_and_service_key_valid` and the four fixed booleans.
- Produces: a silent PASS for a valid service-key document and `status_schema_invalid` for mismatched/extra/missing fields.

- [x] Add tests that a valid service-key document is silent PASS and that secret-like, extra, missing, wrong-type, false, or code/detail-mismatched shapes are INVALID.
- [x] Run `python -m unittest tests.workflows.test_aurora_gateway_health_alert_export -v` in Studio OS; expected RED.
- [x] Add the PASS code to `CODE_STATUS` and make `detailsAreValid(name, code, details)` accept the four booleans only for `database_and_service_key_valid`; retain the legacy JWT shape for legacy codes.
- [x] Rerun the focused Studio OS test; expected GREEN.

### Task 4: Cross-repository verification and stop gate

**Files:**
- Verify only; no additional files.

**Interfaces:**
- Consumes: both local implementations.
- Produces: local evidence only; no publication or deployment.

- [x] Run Aurora focused tests, then its complete `unittest` suite, `git diff --check`, and exact changed-file review.
- [x] Run Studio OS focused workflow tests, then its complete `unittest` suite, `git diff --check`, and exact changed-file review.
- [x] Confirm neither repository tracks/stages secrets and neither has staged changes.
- [x] Stop and report the two-repository diff and validation results. Await separate authorization for commits/push, Git bundle/FNOS producer deployment, and n8n workflow import/reload.
