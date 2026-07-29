# Stack Health Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only NAS command that reports Docker, SQLite, token refresh, Mihomo/SG, model-list, and real-chat health in Chinese text or JSON without exposing credentials.

**Architecture:** Create one standalone `scripts/check_stack_health.py` entrypoint organized around independent `CheckResult` producers. Reuse only side-effect-free helpers from `refresh_chatgpt_access_token.py`; keep secrets in local variables, return only allowlisted details, and continue running checks after isolated failures.

**Tech Stack:** Python 3 standard library (`argparse`, `dataclasses`, `json`, `sqlite3`, `subprocess`, `urllib`, `unittest`, `unittest.mock`), Docker CLI, New API SQLite, Mihomo control/HTTP proxy APIs.

## Global Constraints

- Run from the fnOS NAS project directory; WSL remains limited to static validation.
- Use Python standard library only; add no package, service, container, or system dependency.
- Default execution includes one real New API chat validation.
- Default output is a Chinese summary; `--json` emits one UTF-8 JSON object.
- The command is read-only: do not modify files, SQLite, tokens, containers, proxy selection, Compose, or cron.
- Never output channel tokens, client tokens, session tokens, token hashes, chat prompts, chat content, raw HTTP bodies, container environment variables, or tracebacks.
- Expected public models are exactly `gpt-5-6-pro` and `gpt-5-6-thinking`.
- Mihomo must report `GLOBAL`, and Cloudflare trace accessed through its HTTP proxy must report `loc=SG`.
- A structurally valid HTTP 200 completion with empty content is `WARN`, not authentication failure.
- Exit `1` when any check is `FAIL`; exit `0` for `PASS` or `WARN`.
- Do not deploy to NAS or alter live state as part of local implementation; live deployment requires separate approval.

---

## File Structure

- Create `scripts/check_stack_health.py`: result model, redaction, local checks, network checks, orchestration, CLI, and renderers.
- Create `tests/test_check_stack_health.py`: isolated standard-library tests with no Docker, network, NAS, or real credentials.
- Modify `README.md`: short user-facing command and status/exit-code explanation.
- Modify `docs/fnos_deployment.md`: complete NAS usage, JSON example, checked boundaries, and troubleshooting semantics.
- Do not modify `scripts/refresh_chatgpt_access_token.py` or its existing tests.

---

### Task 1: Result Model, Safe Rendering, and Overall Status

**Files:**
- Create: `scripts/check_stack_health.py`
- Create: `tests/test_check_stack_health.py`

**Interfaces:**
- Produces: `CheckResult(name: str, status: str, summary: str, details: dict[str, object])`
- Produces: `safe_text(value: object, secrets: tuple[str, ...] = (), limit: int = 240) -> str`
- Produces: `overall_status(results: list[CheckResult]) -> str`
- Produces: `build_report(results: list[CheckResult], checked_at: str) -> dict[str, object]`
- Produces: `render_human(report: dict[str, object]) -> str`
- Produces: `render_json(report: dict[str, object]) -> str`

- [ ] **Step 1: Write the failing result/rendering tests**

Create `tests/test_check_stack_health.py` with the same import-by-path pattern used by the existing refresh tests:

```python
import importlib.util
import json
from pathlib import Path
import sys
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "check_stack_health.py"
)
SPEC = importlib.util.spec_from_file_location("stack_health", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ResultTests(unittest.TestCase):
    def test_overall_status_uses_worst_result(self):
        results = [
            MODULE.CheckResult("a", "PASS", "ok", {}),
            MODULE.CheckResult("b", "WARN", "warning", {}),
        ]
        self.assertEqual(MODULE.overall_status(results), "WARN")
        results.append(MODULE.CheckResult("c", "FAIL", "failed", {}))
        self.assertEqual(MODULE.overall_status(results), "FAIL")

    def test_safe_text_redacts_known_secret_and_limits_length(self):
        secret = "secret-value"
        value = f"prefix {secret} " + "x" * 500
        result = MODULE.safe_text(value, (secret,), limit=40)
        self.assertNotIn(secret, result)
        self.assertLessEqual(len(result), 40)

    def test_json_and_human_renderers_are_stable(self):
        results = [
            MODULE.CheckResult(
                "containers",
                "PASS",
                "4/4 运行，重启次数均为 0",
                {"running": 4},
            )
        ]
        report = MODULE.build_report(
            results,
            "2026-07-29T12:00:00+08:00",
        )
        parsed = json.loads(MODULE.render_json(report))
        self.assertEqual(parsed["overall"], "PASS")
        self.assertEqual(parsed["checks"][0]["name"], "containers")
        human = MODULE.render_human(report)
        self.assertIn("[通过] 容器", human)
        self.assertIn("总体：通过", human)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new test to verify it fails because the script is absent**

Run:

```powershell
python -m unittest tests.test_check_stack_health -v
```

Expected: import failure for `scripts/check_stack_health.py`.

- [ ] **Step 3: Implement the result model and renderers**

Create `scripts/check_stack_health.py` with this foundation:

```python
#!/usr/bin/env python3
"""Read-only health check for the local_aurora_api fnOS stack."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Callable


STATUS_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}
STATUS_LABELS = {"PASS": "通过", "WARN": "警告", "FAIL": "失败"}
CHECK_LABELS = {
    "containers": "容器",
    "database": "数据库与 Token",
    "refresh_log": "续期日志",
    "mihomo": "代理",
    "models": "模型",
    "chat": "聊天",
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    summary: str
    details: dict[str, object]


def safe_text(
    value: object,
    secrets: tuple[str, ...] = (),
    limit: int = 240,
) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:limit]


def overall_status(results: list[CheckResult]) -> str:
    if not results:
        return "FAIL"
    return max(results, key=lambda item: STATUS_ORDER[item.status]).status


def build_report(
    results: list[CheckResult],
    checked_at: str,
) -> dict[str, object]:
    return {
        "checked_at": checked_at,
        "overall": overall_status(results),
        "checks": [asdict(result) for result in results],
    }


def render_json(report: dict[str, object]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True)


def render_human(report: dict[str, object]) -> str:
    lines = []
    for check in report["checks"]:
        status = str(check["status"])
        name = str(check["name"])
        lines.append(
            f"[{STATUS_LABELS[status]}] "
            f"{CHECK_LABELS[name]}：{check['summary']}"
        )
    lines.append("")
    lines.append(f"总体：{STATUS_LABELS[str(report['overall'])]}")
    return "\n".join(lines)
```

Reject unknown statuses by validating `status in STATUS_ORDER` in `CheckResult.__post_init__`; raise `ValueError("unknown check status")` without including user data.

- [ ] **Step 4: Run the result tests**

Run:

```powershell
python -m unittest tests.test_check_stack_health.ResultTests -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit the result model**

```powershell
git add -- scripts/check_stack_health.py tests/test_check_stack_health.py
git diff --cached --check
git commit -m "feat: add health check result model"
```

---

### Task 2: Docker, SQLite/Token, and Refresh-Log Checks

**Files:**
- Modify: `scripts/check_stack_health.py`
- Modify: `tests/test_check_stack_health.py`

**Interfaces:**
- Consumes: `CheckResult`, `safe_text`
- Produces: `run_command(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]`
- Produces: `check_containers(run: Callable[..., subprocess.CompletedProcess[str]] = run_command) -> CheckResult`
- Produces: `check_database(root: Path, channel_id: int, now: int) -> tuple[CheckResult, str | None, tuple[str, ...]]`
- Produces: `check_refresh_log(root: Path, secrets: tuple[str, ...] = ()) -> CheckResult`
- `check_database` returns the enabled New API client token only for downstream in-memory requests; the third tuple item contains known secrets for redaction. Neither value may enter `CheckResult.details`.

- [ ] **Step 1: Add failing tests for Docker state**

Add imports `subprocess` and `unittest.mock` to the test file, then add:

```python
class ContainerTests(unittest.TestCase):
    def test_all_expected_containers_are_running_without_restarts(self):
        payload = [
            {
                "Name": "/aurora",
                "State": {"Status": "running"},
                "RestartCount": 0,
            },
            {
                "Name": "/new-api",
                "State": {"Status": "running"},
                "RestartCount": 0,
            },
            {
                "Name": "/mihomo",
                "State": {"Status": "running"},
                "RestartCount": 0,
            },
            {
                "Name": "/metacubexd",
                "State": {"Status": "running"},
                "RestartCount": 0,
            },
        ]
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(payload),
                stderr="",
            )
        )
        result = MODULE.check_containers(run)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.details["running"], 4)

    def test_stopped_or_restarted_container_fails(self):
        payload = [
            {
                "Name": "/aurora",
                "State": {"Status": "exited"},
                "RestartCount": 1,
            }
        ]
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(payload),
                stderr="",
            )
        )
        result = MODULE.check_containers(run)
        self.assertEqual(result.status, "FAIL")
        self.assertNotIn("secret", result.summary.lower())
```

- [ ] **Step 2: Add failing SQLite/token tests**

Add `base64`, `sqlite3`, `tempfile`, and `time` imports and helpers:

```python
def make_token(exp: int, marker: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "marker": marker}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.signature-{marker}"


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        db_dir = self.root / "data" / "new-api"
        db_dir.mkdir(parents=True)
        self.database = db_dir / "one-api.db"
        self.now = 1_700_000_000
        self.channel_token = make_token(
            self.now + 7 * 24 * 3600,
            "channel",
        )
        with sqlite3.connect(self.database) as database:
            database.execute(
                "CREATE TABLE channels "
                "(id INTEGER PRIMARY KEY, key TEXT, status INTEGER)"
            )
            database.execute(
                "CREATE TABLE tokens "
                "(id INTEGER PRIMARY KEY, key TEXT, status INTEGER, "
                "expired_time INTEGER, unlimited_quota INTEGER, "
                "remain_quota INTEGER)"
            )
            database.execute(
                "INSERT INTO channels VALUES (1, ?, 1)",
                (self.channel_token,),
            )
            database.execute(
                "INSERT INTO tokens VALUES (1, ?, 1, -1, 1, 0)",
                ("client-token-value",),
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_healthy_database_returns_client_token_outside_details(self):
        result, client_token, secrets = MODULE.check_database(
            self.root,
            1,
            self.now,
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(client_token, "client-token-value")
        self.assertIn(self.channel_token, secrets)
        serialized = json.dumps(result.details)
        self.assertNotIn(self.channel_token, serialized)
        self.assertNotIn("client-token-value", serialized)

    def test_token_inside_threshold_warns(self):
        near = make_token(self.now + 48 * 3600, "near")
        with sqlite3.connect(self.database) as database:
            database.execute(
                "UPDATE channels SET key = ? WHERE id = 1",
                (near,),
            )
        result, _, _ = MODULE.check_database(self.root, 1, self.now)
        self.assertEqual(result.status, "WARN")

    def test_expired_token_fails(self):
        expired = make_token(self.now - 1, "expired")
        with sqlite3.connect(self.database) as database:
            database.execute(
                "UPDATE channels SET key = ? WHERE id = 1",
                (expired,),
            )
        result, _, _ = MODULE.check_database(self.root, 1, self.now)
        self.assertEqual(result.status, "FAIL")
```

- [ ] **Step 3: Add failing refresh-log tests**

```python
class RefreshLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".secrets").mkdir()
        self.log = self.root / ".secrets" / "token-refresh.log"

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_log_warns(self):
        result = MODULE.check_refresh_log(self.root)
        self.assertEqual(result.status, "WARN")

    def test_latest_successful_event_passes(self):
        self.log.write_text(
            json.dumps(
                {
                    "time": "2026-07-29T04:17:01+0800",
                    "event": "refresh_skipped",
                    "remaining_seconds": 681883,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = MODULE.check_refresh_log(self.root)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.details["event"], "refresh_skipped")

    def test_invalid_line_warns_when_valid_event_exists(self):
        self.log.write_text(
            "not-json\n"
            + json.dumps(
                {
                    "time": "2026-07-29T04:17:01+0800",
                    "event": "refresh_skipped",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = MODULE.check_refresh_log(self.root)
        self.assertEqual(result.status, "WARN")

    def test_refresh_failed_fails_and_redacts_secret(self):
        secret = "known-secret"
        self.log.write_text(
            json.dumps(
                {
                    "time": "2026-07-29T04:17:01+0800",
                    "event": "refresh_failed",
                    "reason": f"request failed {secret}",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = MODULE.check_refresh_log(self.root, (secret,))
        self.assertEqual(result.status, "FAIL")
        self.assertNotIn(secret, json.dumps(result.details))
```

- [ ] **Step 4: Run the new local-check tests and verify failure**

Run:

```powershell
python -m unittest `
  tests.test_check_stack_health.ContainerTests `
  tests.test_check_stack_health.DatabaseTests `
  tests.test_check_stack_health.RefreshLogTests -v
```

Expected: failures because `check_containers`, `check_database`, and `check_refresh_log` are undefined.

- [ ] **Step 5: Implement Docker inspection**

Add imports `json`, `sqlite3`, `subprocess`, and `time`. Implement:

```python
EXPECTED_CONTAINERS = ("aurora", "new-api", "mihomo", "metacubexd")


def run_command(
    args: list[str],
    timeout: int = 20,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def check_containers(
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
) -> CheckResult:
    try:
        completed = run(
            ["docker", "inspect", *EXPECTED_CONTAINERS],
            timeout=20,
        )
        if completed.returncode != 0:
            return CheckResult(
                "containers",
                "FAIL",
                "无法读取预期容器状态",
                {"error": "docker_inspect_failed"},
            )
        inspected = json.loads(completed.stdout)
        states = {}
        for item in inspected:
            name = str(item.get("Name", "")).lstrip("/")
            if name in EXPECTED_CONTAINERS:
                states[name] = {
                    "state": item.get("State", {}).get("Status"),
                    "restart_count": item.get("RestartCount"),
                }
        healthy = (
            set(states) == set(EXPECTED_CONTAINERS)
            and all(
                value["state"] == "running"
                and value["restart_count"] == 0
                for value in states.values()
            )
        )
        return CheckResult(
            "containers",
            "PASS" if healthy else "FAIL",
            (
                "4/4 运行，重启次数均为 0"
                if healthy
                else "存在缺失、停止或已重启的容器"
            ),
            {"running": sum(
                value["state"] == "running"
                for value in states.values()
            ), "containers": states},
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return CheckResult(
            "containers",
            "FAIL",
            "Docker 状态检查失败",
            {"error": "docker_inspect_error"},
        )
```

Do not include `completed.stderr` in any result.

- [ ] **Step 6: Implement read-only SQLite/token checks**

Load the existing refresh module by sibling import:

```python
import refresh_chatgpt_access_token as token_refresh
```

Implement a read-only database URI and use `token_refresh.jwt_exp` only after reading the enabled channel inside the read-only connection:

```python
def check_database(
    root: Path,
    channel_id: int,
    now: int,
) -> tuple[CheckResult, str | None, tuple[str, ...]]:
    path = root / "data" / "new-api" / "one-api.db"
    channel_token = ""
    client_token = ""
    try:
        with sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=30,
        ) as database:
            integrity = database.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            channel = database.execute(
                "SELECT key FROM channels WHERE id = ? AND status = 1",
                (channel_id,),
            ).fetchone()
            client = database.execute(
                """
                SELECT key FROM tokens
                WHERE status = 1
                  AND (expired_time = -1 OR expired_time > ?)
                  AND (unlimited_quota = 1 OR remain_quota > 0)
                ORDER BY id LIMIT 1
                """,
                (now,),
            ).fetchone()
        if integrity != ("ok",) or channel is None or client is None:
            raise ValueError("required database state is unavailable")
        channel_token = str(channel[0])
        client_token = str(client[0])
        client_token = (
            client_token
            if client_token.startswith("sk-")
            else f"sk-{client_token}"
        )
        remaining = token_refresh.jwt_exp(channel_token) - now
        status = (
            "FAIL"
            if remaining <= 0
            else "WARN"
            if remaining <= 72 * 3600
            else "PASS"
        )
        summary = (
            "渠道 Token 已过期"
            if remaining <= 0
            else f"数据库完整，Token 剩余 {remaining // 3600} 小时"
        )
        return (
            CheckResult(
                "database",
                status,
                summary,
                {
                    "integrity": "ok",
                    "remaining_seconds": remaining,
                    "expires_at": datetime.fromtimestamp(
                        now + remaining
                    ).astimezone().isoformat(timespec="seconds"),
                },
            ),
            client_token,
            (channel_token, client_token),
        )
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        token_refresh.RefreshError,
    ):
        secrets = tuple(
            value for value in (channel_token, client_token) if value
        )
        return (
            CheckResult(
                "database",
                "FAIL",
                "数据库、渠道 Token 或客户端令牌检查失败",
                {"error": "database_state_invalid"},
            ),
            None,
            secrets,
        )
```

Do not call `token_refresh.read_channel_token` or `read_client_token`, because those helpers open SQLite without `mode=ro`.

- [ ] **Step 7: Implement allowlisted refresh-log parsing**

Define the exact field allowlist and implement:

```python
LOG_FIELDS = {
    "time",
    "event",
    "remaining_seconds",
    "channel_id",
    "reason",
    "previous_exp",
    "new_exp",
    "extension_seconds",
}


def check_refresh_log(
    root: Path,
    secrets: tuple[str, ...] = (),
) -> CheckResult:
    path = root / ".secrets" / "token-refresh.log"
    if not path.exists() or path.stat().st_size == 0:
        return CheckResult(
            "refresh_log",
            "WARN",
            "续期日志尚不存在或为空",
            {"event": None},
        )
    records = []
    invalid_lines = 0
    try:
        for line in path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if isinstance(raw, dict):
                record = {
                    key: (
                        safe_text(raw[key], secrets)
                        if key == "reason"
                        else raw[key]
                    )
                    for key in LOG_FIELDS
                    if key in raw
                }
                records.append(record)
    except OSError:
        return CheckResult(
            "refresh_log",
            "FAIL",
            "无法读取续期日志",
            {"error": "refresh_log_read_failed"},
        )
    if not records:
        return CheckResult(
            "refresh_log",
            "FAIL",
            "续期日志中没有合法 JSON 事件",
            {"invalid_lines": invalid_lines},
        )
    latest = records[-1]
    event = latest.get("event")
    if event == "refresh_failed":
        status = "FAIL"
    elif event in {"refresh_skipped", "refresh_succeeded"}:
        status = "WARN" if invalid_lines else "PASS"
    else:
        status = "FAIL"
    return CheckResult(
        "refresh_log",
        status,
        f"最新事件为 {event or 'unknown'}",
        {**latest, "invalid_lines": invalid_lines},
    )
```

- [ ] **Step 8: Run all Task 2 tests**

Run:

```powershell
python -m unittest `
  tests.test_check_stack_health.ContainerTests `
  tests.test_check_stack_health.DatabaseTests `
  tests.test_check_stack_health.RefreshLogTests -v
```

Expected: all Task 2 tests pass.

- [ ] **Step 9: Run existing token-refresh regression tests**

Run:

```powershell
python -m unittest tests.test_refresh_chatgpt_access_token -v
```

Expected: all 6 existing tests pass; the existing refresh script remains unchanged.

- [ ] **Step 10: Commit local checks**

```powershell
git add -- scripts/check_stack_health.py tests/test_check_stack_health.py
git diff --cached --check
git commit -m "feat: check local stack health"
```

---

### Task 3: Mihomo/SG, Model Range, and Real Chat Checks

**Files:**
- Modify: `scripts/check_stack_health.py`
- Modify: `tests/test_check_stack_health.py`

**Interfaces:**
- Consumes: `CheckResult`, `safe_text`, `run_command`, in-memory client token
- Produces: `discover_mihomo_endpoints(run=run_command) -> tuple[str, str]`
- Produces: `check_mihomo(fetch_json, fetch_text, run=run_command) -> CheckResult`
- Produces: `check_models(client_token: str, request: Callable = token_refresh.request_json) -> CheckResult`
- Produces: `check_chat(client_token: str, request: Callable = token_refresh.request_json) -> CheckResult`
- The injected request callables let unit tests avoid all real network access.

- [ ] **Step 1: Add failing Mihomo tests**

```python
class MihomoTests(unittest.TestCase):
    def test_global_mode_and_sg_exit_pass(self):
        fetch_json = mock.Mock(
            side_effect=[
                {"mode": "global"},
                {"now": "Singapore Node"},
            ]
        )
        fetch_text = mock.Mock(return_value="ip=203.0.113.1\nloc=SG\n")
        with mock.patch.object(
            MODULE,
            "discover_mihomo_endpoints",
            return_value=(
                "http://172.19.0.3:9090",
                "http://172.19.0.3:7890",
            ),
        ):
            result = MODULE.check_mihomo(fetch_json, fetch_text)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.details["mode"], "GLOBAL")
        self.assertEqual(result.details["country"], "SG")

    def test_non_global_or_non_sg_fails(self):
        fetch_json = mock.Mock(
            side_effect=[
                {"mode": "rule"},
                {"now": "Other Node"},
            ]
        )
        fetch_text = mock.Mock(return_value="loc=US\n")
        with mock.patch.object(
            MODULE,
            "discover_mihomo_endpoints",
            return_value=(
                "http://172.19.0.3:9090",
                "http://172.19.0.3:7890",
            ),
        ):
            result = MODULE.check_mihomo(fetch_json, fetch_text)
        self.assertEqual(result.status, "FAIL")
```

- [ ] **Step 2: Add failing model-range tests**

```python
class ModelTests(unittest.TestCase):
    def test_exact_models_pass(self):
        request = mock.Mock(
            return_value={
                "data": [
                    {"id": "gpt-5-6-thinking"},
                    {"id": "gpt-5-6-pro"},
                ]
            }
        )
        result = MODULE.check_models("client-secret", request)
        self.assertEqual(result.status, "PASS")

    def test_extra_or_missing_model_fails(self):
        request = mock.Mock(
            return_value={
                "data": [
                    {"id": "gpt-5-6-pro"},
                    {"id": "gpt-image-2"},
                ]
            }
        )
        result = MODULE.check_models("client-secret", request)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(
            result.details["model_ids"],
            ["gpt-5-6-pro", "gpt-image-2"],
        )
```

- [ ] **Step 3: Add failing real-chat tests**

```python
class ChatTests(unittest.TestCase):
    def test_nonempty_pro_completion_passes(self):
        request = mock.Mock(
            return_value={
                "choices": [
                    {"message": {"content": "OK"}, "finish_reason": "stop"}
                ]
            }
        )
        result = MODULE.check_chat("client-secret", request)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.details["model"], "gpt-5-6-pro")

    def test_empty_completion_warns(self):
        request = mock.Mock(
            return_value={
                "choices": [
                    {"message": {"content": ""}, "finish_reason": "stop"}
                ]
            }
        )
        result = MODULE.check_chat("client-secret", request)
        self.assertEqual(result.status, "WARN")
        self.assertNotIn("content", result.details)

    def test_thinking_fallback_warns(self):
        request = mock.Mock(
            side_effect=[
                MODULE.token_refresh.RefreshError("pro failed"),
                {
                    "choices": [
                        {
                            "message": {"content": "OK"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            ]
        )
        result = MODULE.check_chat("client-secret", request)
        self.assertEqual(result.status, "WARN")
        self.assertEqual(result.details["model"], "gpt-5-6-thinking")

    def test_both_models_fail_without_leaking_token(self):
        secret = "client-secret"
        request = mock.Mock(
            side_effect=MODULE.token_refresh.RefreshError(
                f"request failed {secret}"
            )
        )
        result = MODULE.check_chat(secret, request)
        self.assertEqual(result.status, "FAIL")
        self.assertNotIn(secret, json.dumps(result.details))
        self.assertNotIn(secret, result.summary)
```

- [ ] **Step 4: Run Task 3 tests and verify failure**

Run:

```powershell
python -m unittest `
  tests.test_check_stack_health.MihomoTests `
  tests.test_check_stack_health.ModelTests `
  tests.test_check_stack_health.ChatTests -v
```

Expected: failures because network-check functions are undefined.

- [ ] **Step 5: Implement Mihomo endpoint discovery and safe HTTP helpers**

Add `urllib.error` and `urllib.request` imports. Implement:

```python
def discover_mihomo_endpoints(
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
) -> tuple[str, str]:
    completed = run(
        [
            "docker",
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            "mihomo",
        ],
        timeout=15,
    )
    address = completed.stdout.strip()
    if (
        completed.returncode != 0
        or not address
        or any(char not in "0123456789." for char in address)
    ):
        raise RuntimeError("mihomo_address_unavailable")
    return f"http://{address}:9090", f"http://{address}:7890"


def fetch_json_url(url: str, timeout: int = 20) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def fetch_text_via_proxy(
    url: str,
    proxy_url: str,
    timeout: int = 30,
) -> str:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {"http": proxy_url, "https": proxy_url}
        )
    )
    with opener.open(url, timeout=timeout) as response:
        return response.read(64 * 1024).decode("utf-8", "replace")
```

The fixed 64 KiB response limit prevents an upstream HTML page from being retained.

- [ ] **Step 6: Implement Mihomo and SG checks**

```python
def check_mihomo(
    fetch_json: Callable[..., dict[str, object]] = fetch_json_url,
    fetch_text: Callable[..., str] = fetch_text_via_proxy,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_command,
) -> CheckResult:
    try:
        control_url, proxy_url = discover_mihomo_endpoints(run)
        config = fetch_json(f"{control_url}/configs", timeout=20)
        global_proxy = fetch_json(
            f"{control_url}/proxies/GLOBAL",
            timeout=20,
        )
        trace = fetch_text(
            "https://www.cloudflare.com/cdn-cgi/trace",
            proxy_url,
            timeout=30,
        )
        values = dict(
            line.split("=", 1)
            for line in trace.splitlines()
            if "=" in line
        )
        mode = str(config.get("mode", "")).upper()
        selected = safe_text(global_proxy.get("now", ""), limit=120)
        country = str(values.get("loc", "")).upper()
        healthy = mode == "GLOBAL" and country == "SG"
        return CheckResult(
            "mihomo",
            "PASS" if healthy else "FAIL",
            (
                f"GLOBAL / SG / {selected}"
                if healthy
                else "Mihomo 模式或代理出口不符合要求"
            ),
            {
                "mode": mode,
                "selected": selected,
                "country": country,
            },
        )
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        urllib.error.URLError,
        ValueError,
        json.JSONDecodeError,
    ):
        return CheckResult(
            "mihomo",
            "FAIL",
            "Mihomo 控制接口或代理出口检查失败",
            {"error": "mihomo_check_failed"},
        )
```

- [ ] **Step 7: Implement exact model-range validation**

```python
EXPECTED_MODELS = {"gpt-5-6-pro", "gpt-5-6-thinking"}
NEW_API_BASE = "http://127.0.0.1:3000"


def check_models(
    client_token: str,
    request: Callable[..., dict[str, object]] = (
        token_refresh.request_json
    ),
) -> CheckResult:
    try:
        payload = request(
            f"{NEW_API_BASE}/v1/models",
            client_token,
            timeout=30,
        )
        model_ids = sorted(
            item.get("id")
            for item in payload.get("data", [])
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
        )
        healthy = set(model_ids) == EXPECTED_MODELS
        return CheckResult(
            "models",
            "PASS" if healthy else "FAIL",
            (
                "模型范围严格等于 pro、thinking"
                if healthy
                else "模型范围与正式配置不一致"
            ),
            {"model_ids": model_ids},
        )
    except (
        AttributeError,
        TypeError,
        token_refresh.RefreshError,
    ):
        return CheckResult(
            "models",
            "FAIL",
            "New API 模型检查失败",
            {"error": "model_request_failed"},
        )
```

- [ ] **Step 8: Implement real-chat validation without response leakage**

```python
def check_chat(
    client_token: str,
    request: Callable[..., dict[str, object]] = (
        token_refresh.request_json
    ),
) -> CheckResult:
    failures = 0
    for model in ("gpt-5-6-pro", "gpt-5-6-thinking"):
        marker = f"AURORA-HEALTH-{time.time_ns()}"
        try:
            payload = request(
                f"{NEW_API_BASE}/v1/chat/completions",
                client_token,
                payload={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"只回答：{marker}",
                        }
                    ],
                    "stream": False,
                },
                timeout=180,
            )
            choices = payload.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                failures += 1
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                failures += 1
                continue
            message = choice.get("message")
            content = (
                message.get("content")
                if isinstance(message, dict)
                else None
            )
            if not isinstance(content, str):
                failures += 1
                continue
            if model == "gpt-5-6-pro" and content:
                status = "PASS"
                summary = "pro 返回结构合法的非空 completion"
            elif content:
                status = "WARN"
                summary = "pro 失败，thinking 返回结构合法 completion"
            else:
                status = "WARN"
                summary = f"{model} 返回结构合法的空 completion"
            return CheckResult(
                "chat",
                status,
                summary,
                {
                    "model": model,
                    "content_empty": not bool(content),
                    "fallback_used": model == "gpt-5-6-thinking",
                },
            )
        except (
            AttributeError,
            TypeError,
            token_refresh.RefreshError,
        ):
            failures += 1
    return CheckResult(
        "chat",
        "FAIL",
        "pro 与 thinking 聊天检查均失败",
        {"attempts_failed": failures},
    )
```

Never put the marker, content, request body, exception text, or token into `details`.

- [ ] **Step 9: Run Task 3 tests**

Run:

```powershell
python -m unittest `
  tests.test_check_stack_health.MihomoTests `
  tests.test_check_stack_health.ModelTests `
  tests.test_check_stack_health.ChatTests -v
```

Expected: all Task 3 tests pass.

- [ ] **Step 10: Commit network checks**

```powershell
git add -- scripts/check_stack_health.py tests/test_check_stack_health.py
git diff --cached --check
git commit -m "feat: check gateway and proxy health"
```

---

### Task 4: CLI Orchestration, Output Safety, and Documentation

**Files:**
- Modify: `scripts/check_stack_health.py`
- Modify: `tests/test_check_stack_health.py`
- Modify: `README.md`
- Modify: `docs/fnos_deployment.md`

**Interfaces:**
- Consumes: all check functions and renderers from Tasks 1-3
- Produces: `parse_args(argv: list[str] | None = None) -> argparse.Namespace`
- Produces: `run_health_check(root: Path, channel_id: int, now: int | None = None) -> dict[str, object]`
- Produces: `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Add failing orchestration and CLI tests**

Add:

```python
class CliTests(unittest.TestCase):
    def test_run_health_check_collects_all_six_checks(self):
        pass_result = lambda name: MODULE.CheckResult(
            name,
            "PASS",
            "ok",
            {},
        )
        with (
            mock.patch.object(
                MODULE,
                "check_containers",
                return_value=pass_result("containers"),
            ),
            mock.patch.object(
                MODULE,
                "check_database",
                return_value=(
                    pass_result("database"),
                    "client-secret",
                    ("channel-secret", "client-secret"),
                ),
            ),
            mock.patch.object(
                MODULE,
                "check_refresh_log",
                return_value=pass_result("refresh_log"),
            ),
            mock.patch.object(
                MODULE,
                "check_mihomo",
                return_value=pass_result("mihomo"),
            ),
            mock.patch.object(
                MODULE,
                "check_models",
                return_value=pass_result("models"),
            ),
            mock.patch.object(
                MODULE,
                "check_chat",
                return_value=pass_result("chat"),
            ),
        ):
            report = MODULE.run_health_check(
                Path("/example"),
                1,
                now=1_700_000_000,
            )
        self.assertEqual(
            [item["name"] for item in report["checks"]],
            [
                "containers",
                "database",
                "refresh_log",
                "mihomo",
                "models",
                "chat",
            ],
        )
        serialized = json.dumps(report)
        self.assertNotIn("channel-secret", serialized)
        self.assertNotIn("client-secret", serialized)

    def test_main_json_output_and_exit_codes(self):
        report = {
            "checked_at": "2026-07-29T12:00:00+08:00",
            "overall": "FAIL",
            "checks": [],
        }
        with mock.patch.object(
            MODULE,
            "run_health_check",
            return_value=report,
        ):
            with mock.patch("builtins.print") as output:
                exit_code = MODULE.main(["--json"])
        self.assertEqual(exit_code, 1)
        json.loads(output.call_args.args[0])

    def test_main_warn_returns_zero(self):
        report = {
            "checked_at": "2026-07-29T12:00:00+08:00",
            "overall": "WARN",
            "checks": [],
        }
        with mock.patch.object(
            MODULE,
            "run_health_check",
            return_value=report,
        ):
            exit_code = MODULE.main([])
        self.assertEqual(exit_code, 0)
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run:

```powershell
python -m unittest tests.test_check_stack_health.CliTests -v
```

Expected: failures because `run_health_check` and `main` are not implemented.

- [ ] **Step 3: Implement orchestration with dependency-aware failures**

```python
def dependency_failure(name: str, dependency: str) -> CheckResult:
    return CheckResult(
        name,
        "FAIL",
        f"缺少前置检查：{dependency}",
        {"error": "dependency_failed", "dependency": dependency},
    )


def run_health_check(
    root: Path,
    channel_id: int,
    now: int | None = None,
) -> dict[str, object]:
    checked_epoch = int(time.time()) if now is None else now
    checked_at = datetime.fromtimestamp(
        checked_epoch
    ).astimezone().isoformat(timespec="seconds")
    containers = check_containers()
    database, client_token, secrets = check_database(
        root,
        channel_id,
        checked_epoch,
    )
    refresh_log = check_refresh_log(root, secrets)
    mihomo = check_mihomo()
    if client_token is None:
        models = dependency_failure("models", "database")
        chat = dependency_failure("chat", "database")
    else:
        models = check_models(client_token)
        chat = check_chat(client_token)
    return build_report(
        [
            containers,
            database,
            refresh_log,
            mihomo,
            models,
            chat,
        ],
        checked_at,
    )
```

The `secrets` tuple is used only for log redaction and is discarded before building the report.

- [ ] **Step 4: Implement argument parsing and main**

```python
def parse_args(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查 local_aurora_api 运行状态"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--channel-id", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_health_check(
        args.root.resolve(),
        args.channel_id,
    )
    print(render_json(report) if args.json else render_human(report))
    return 1 if report["overall"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run all health-check tests**

Run:

```powershell
python -m unittest tests.test_check_stack_health -v
```

Expected: all health-check tests pass.

- [ ] **Step 6: Add README usage**

After the existing “可选：定时续期” section in `README.md`, add:

```markdown
## 一键健康检查

在 NAS 项目目录运行：

```bash
python3 scripts/check_stack_health.py
```

脚本只读检查四个容器、SQLite 与 Token、续期日志、Mihomo GLOBAL/新加坡出口、正式模型范围和一次真实聊天链路。使用 `--json` 可输出机器可读结果；存在失败项时退出码为 `1`。脚本不会输出凭据或聊天正文。
```

Keep README content user-facing; do not add implementation internals or canary evidence.

- [ ] **Step 7: Add fnOS deployment details**

In `docs/fnos_deployment.md`, add a new subsection immediately before “11.2 mihomo 保持 GLOBAL 模式”:

```markdown
### 11.2 一键只读健康检查

在项目根目录运行：

```bash
python3 scripts/check_stack_health.py
python3 scripts/check_stack_health.py --json
```

检查范围包括：

- 四个生产容器均为 running 且重启次数为 0；
- New API SQLite 完整性、渠道 Token 剩余时间和可用客户端令牌；
- 最新定时续期事件；
- Mihomo GLOBAL 模式、当前节点和经代理确认的 `SG` 出口；
- 对外模型严格等于 `gpt-5-6-pro`、`gpt-5-6-thinking`；
- 一次真实 New API 聊天请求及 OpenAI completion 结构。

只有通过或警告时退出码为 `0`；任一失败项使退出码为 `1`。合法 HTTP 200 空 completion 会显示警告，但不会被误判为鉴权失败。

脚本不会修改数据库、Token、容器、节点选择或配置，也不会输出凭据和聊天正文。
```

Renumber the following subsections from 11.2-11.5 to 11.3-11.6, and update any same-document references affected by the renumbering.

- [ ] **Step 8: Run full Python verification**

Run:

```powershell
python -m unittest discover -s tests -v
python -m py_compile `
  scripts/check_stack_health.py `
  scripts/refresh_chatgpt_access_token.py
```

Expected: all new and existing tests pass; both scripts compile.

- [ ] **Step 9: Run repository hygiene checks**

Run:

```powershell
git diff --check
git check-ignore -v `
  .env `
  .secrets/ `
  data/ `
  TODO.md `
  DEVLOG.md
rg -n "eyJ[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}" `
  README.md DESIGN.md docs scripts tests
git -c core.quotepath=false status --short
```

Run this read-only relative-link validator:

```powershell
@'
import pathlib
import re

files = [
    pathlib.Path("README.md"),
    pathlib.Path("DESIGN.md"),
    *pathlib.Path("docs").rglob("*.md"),
]
missing = []
for file in files:
    text = file.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)#]+)", text):
        if "://" in target or target.startswith(("mailto:", "/")):
            continue
        path = (file.parent / target).resolve()
        if not path.exists():
            missing.append(f"{file}: {target}")
if missing:
    raise SystemExit("\n".join(missing))
print("MARKDOWN_LINKS=ok")
'@ | python -
```

Expected:

- `git diff --check` has no output.
- All listed local/sensitive paths are ignored.
- Every relative Markdown target resolves from its containing file.
- Credential scan has no real credential match; documented placeholder text is reviewed manually.
- Only the health-check script, tests, README, deployment guide, and already-approved plan/spec files are changed.

- [ ] **Step 10: Commit the completed health check**

```powershell
git add -- `
  scripts/check_stack_health.py `
  tests/test_check_stack_health.py `
  README.md `
  docs/fnos_deployment.md
git diff --cached --check
git commit -m "feat: add one-command stack health check"
```

Do not push or deploy to NAS without a separate explicit user instruction.

---

## Final Local Acceptance

- [ ] Run `python -m unittest discover -s tests -v` once more from a clean index.
- [ ] Run `python -m py_compile scripts/check_stack_health.py scripts/refresh_chatgpt_access_token.py`.
- [ ] Run `git diff --check` and inspect `git status --short --branch`.
- [ ] Confirm the output model never serializes `channel_token`, `client_token`, chat prompt, chat content, raw HTTP body, container environment, or traceback.
- [ ] Confirm no command in the implementation writes to SQLite, Docker, Mihomo, token files, or cron.
- [ ] Report that NAS live behavior remains unverified until deployment is separately approved.
