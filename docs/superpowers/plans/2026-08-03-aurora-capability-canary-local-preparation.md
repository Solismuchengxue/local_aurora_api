# Aurora Capability Canary Local Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Windows 仓库中实现一个默认不能访问生产端口、必须显式批准真实调用的 Aurora 多模态 canary 验收工具和隔离 Compose 定义，为后续 FNOS canary 提供可测试、可回退且不泄露凭据的本地制品。

**Architecture:** 新增一个标准库 Python canary 工具，分别对固定 loopback 端口上的 Aurora canary 和 New API canary 执行同一能力矩阵，只输出固定枚举、布尔值、计数和 UTC 时间。新增单独的 Compose 文件定义 canary 容器、数据和只读 session token 挂载；生产 `docker-compose.yml`、健康生产器、cron、n8n 和正式文档保持不变。

**Tech Stack:** Python 3 标准库（`argparse`、`base64`、`dataclasses`、`http.client`、`json`、`pathlib`、`struct`、`tempfile`、`unittest`、`urllib`、`wave`、`zlib`）、Docker Compose v2 静态解析、JSON Schema Draft 2020-12 文档（不新增校验库）。

## Global Constraints

- 本计划只实施 Windows 本地代码、测试、候选 Compose 和候选 runbook；不连接 FNOS，不拉取或启动镜像，不调用真实 API。
- 生产 `docker-compose.yml`、`scripts/refresh_chatgpt_access_token.py`、`scripts/write_n8n_health_status.py`、`DESIGN.md`、`README.md` 和现有 n8n 现场状态均不得修改。
- canary 工具只允许访问 `http://127.0.0.1:18080` 与 `http://127.0.0.1:13000`；必须拒绝生产 `8080`/`3000`、非 loopback 主机、HTTPS 外站和任意自定义 URL。
- 缺少 `--allow-real-api` 时，工具必须在建立任何网络连接前以退出码 `2` 失败关闭。
- 真实凭据只从被忽略的 `.env.canary` 和 `.secrets/canary/new_api_client_token.txt` 读取；不得通过 CLI 参数、日志、报告、异常正文或测试 fixture 输出。
- canary Aurora 只读挂载单个 `.secrets/canary/session_tokens.txt`；不得挂载整个 `.secrets/`，不得把文件改成 world-readable。
- 报告只含固定枚举、布尔值、整数和 UTC 时间；不得包含 prompt、回复文本、file id、签名 URL、base64、音频、图片、Token、Cookie、邮箱、连接串、原始日志或异常正文。
- 单次响应体最大读取 `32 MiB`，单次 JSON 文本最大 `8 MiB`，最终报告最大 `32 KiB`。
- 使用合成内容；不读取用户业务文件、真实聊天、私人图片或私人音频。
- 不新增第三方 Python 包、系统工具、Skill、Plugin、MCP 或生产依赖。
- Git 暂存、commit、push 分别遵循用户独立授权；本计划中的 commit 步骤只有在相应 Git 门禁已明确批准时才能执行。

## File Map

- Create `docker-compose.canary.yml`: 只定义 Aurora/New API canary，不覆盖或扩展生产 Compose。
- Create `.env.canary.example`: 只列出必填变量名和失败关闭说明，不含真实值。
- Modify `.gitignore`: 忽略 `/.env.canary`，其余 `.secrets/`、`data/` 已由现有规则覆盖。
- Create `tests/test_canary_compose_contract.py`: 用标准库静态锁定服务名、端口、挂载、镜像和生产隔离边界。
- Create `scripts/aurora_capability_canary.py`: 安全门、HTTP/multipart/SSE 传输、能力校验、脱敏报告和 CLI。
- Create `tests/test_aurora_capability_canary.py`: 全部使用内存响应与 mock transport；不得访问网络、Docker 或凭据。
- Create `docs/contracts/aurora-capability-canary-report-v1.schema.json`: consumer-neutral 脱敏报告合同。
- Create `docs/aurora_capability_canary.md`: 明确候选状态、FNOS 后续门禁、真实调用副作用和运行/回退顺序。
- Modify `docs/superpowers/specs/2026-08-03-aurora-capability-first-upgrade-design.md`: 只增加指向本计划与报告合同的相对链接，不改变已批准架构。

---

### Task 1: Canary Compose Isolation Contract

**Files:**
- Create: `docker-compose.canary.yml`
- Create: `.env.canary.example`
- Modify: `.gitignore`
- Create: `tests/test_canary_compose_contract.py`

**Interfaces:**
- Consumes: 生产外部网络名 `aurora-stack_default`；现有固定 New API digest `calciumion/new-api@sha256:428018a37c0b26c163a3367c18401161707cd0e08d0f26a3dde9ff0caa05e34c`。
- Produces: canary 服务 `aurora-canary`、`new-api-canary`；loopback 端口 `18080`、`13000`；后续工具依赖的 `.env.canary` 键 `AURORA_CANARY_IMAGE`、`AURORA_CANARY_AUTHORIZATION`、`NEW_API_CANARY_SESSION_SECRET`。

- [ ] **Step 1: Write the failing Compose contract test**

Create `tests/test_canary_compose_contract.py`:

```python
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.canary.yml"
ENV_EXAMPLE = ROOT / ".env.canary.example"


class CanaryComposeContractTests(unittest.TestCase):
    def test_canary_compose_is_isolated_and_fail_closed(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("name: aurora-capability-canary", text)
        self.assertRegex(text, r"(?m)^  aurora-canary:$")
        self.assertRegex(text, r"(?m)^  new-api-canary:$")
        self.assertNotRegex(text, r"(?m)^  (aurora|new-api|mihomo|dashboard):$")
        self.assertIn('"127.0.0.1:18080:8080"', text)
        self.assertIn('"127.0.0.1:13000:3000"', text)
        self.assertIn("ENABLE_EXTERNAL_TOKEN: \"false\"", text)
        self.assertIn("target: /session_tokens.txt", text)
        self.assertIn("read_only: true", text)
        self.assertNotIn("/vol1/1000/Solis_Aurora_Gateway/data/new-api", text)
        self.assertNotIn("/var/run/docker.sock", text)
        self.assertNotIn("network_mode: host", text)
        self.assertIn("name: aurora-stack_default", text)
        self.assertIn("external: true", text)

    def test_candidate_image_is_required_and_new_api_stays_on_baseline(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn(
            "image: ${AURORA_CANARY_IMAGE:?set an approved immutable Aurora digest}",
            text,
        )
        self.assertIn(
            "calciumion/new-api@sha256:428018a37c0b26c163a3367c18401161707cd0e08d0f26a3dde9ff0caa05e34c",
            text,
        )
        self.assertNotIn(":latest", text)

    def test_example_lists_names_only_and_real_file_is_ignored(self):
        example = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertEqual(
            [line for line in example.splitlines() if line and not line.startswith("#")],
            [
                "AURORA_CANARY_IMAGE=",
                "AURORA_CANARY_AUTHORIZATION=",
                "NEW_API_CANARY_SESSION_SECRET=",
            ],
        )
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertRegex(ignore, r"(?m)^/\.env\.canary$")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
python -m unittest tests.test_canary_compose_contract -v
```

Expected: FAIL because `docker-compose.canary.yml` and `.env.canary.example` do not exist.

- [ ] **Step 3: Add the minimal isolated Compose definition**

Create `docker-compose.canary.yml` exactly with this initial contract:

```yaml
name: aurora-capability-canary

services:
  aurora-canary:
    image: ${AURORA_CANARY_IMAGE:?set an approved immutable Aurora digest}
    container_name: aurora-capability-canary
    profiles: ["canary"]
    ports:
      - "127.0.0.1:18080:8080"
    environment:
      Authorization: ${AURORA_CANARY_AUTHORIZATION:?set the canary service key}
      ENABLE_EXTERNAL_TOKEN: "false"
      PROXY_URL: http://mihomo:7890
      http_proxy: http://mihomo:7890
    volumes:
      - type: bind
        source: ./.secrets/canary/session_tokens.txt
        target: /session_tokens.txt
        read_only: true
    networks:
      - canary
      - aurora-stack
    restart: unless-stopped

  new-api-canary:
    image: calciumion/new-api@sha256:428018a37c0b26c163a3367c18401161707cd0e08d0f26a3dde9ff0caa05e34c
    container_name: new-api-capability-canary
    profiles: ["canary"]
    ports:
      - "127.0.0.1:13000:3000"
    volumes:
      - ./data/canary/new-api:/data
    environment:
      TZ: Asia/Shanghai
      SESSION_SECRET: ${NEW_API_CANARY_SESSION_SECRET:?set the canary session secret}
    depends_on:
      - aurora-canary
    networks:
      - canary
    restart: unless-stopped

networks:
  canary:
    name: aurora-capability-canary
  aurora-stack:
    name: aurora-stack_default
    external: true
```

Create `.env.canary.example`:

```dotenv
# Candidate image must use ghcr.io/aurora-develop/aurora plus a 64-hex sha256 digest.
# Copy this file to ignored .env.canary only during the separately approved FNOS canary gate.
AURORA_CANARY_IMAGE=
AURORA_CANARY_AUTHORIZATION=
NEW_API_CANARY_SESSION_SECRET=
```

Add this line under the existing environment-variable ignore block in `.gitignore`:

```gitignore
/.env.canary
```

- [ ] **Step 4: Run the focused test**

Run:

```powershell
python -m unittest tests.test_canary_compose_contract -v
```

Expected: 3 tests PASS; no Docker command runs.

- [ ] **Step 5: Perform a static Compose parse with non-sensitive values**

Run from WSL or another environment with Docker Compose v2 installed, without `up`, `pull`, or `create`:

```bash
AURORA_CANARY_IMAGE='ghcr.io/aurora-develop/aurora@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
AURORA_CANARY_AUTHORIZATION='non-sensitive-canary-service-key' \
NEW_API_CANARY_SESSION_SECRET='non-sensitive-canary-session-secret' \
docker compose -f docker-compose.canary.yml --profile canary config --format json >/dev/null
```

Expected: exit `0`. This validates syntax only; the fake digest must never be pulled or deployed.

- [ ] **Step 6: Commit only if the Git commit gate is approved**

```powershell
git add -- docker-compose.canary.yml .env.canary.example .gitignore tests/test_canary_compose_contract.py
git diff --cached --check
git commit -m "准备 Aurora 能力 canary 隔离配置"
```

Expected: commit contains exactly the four listed files. If commit authorization is absent, leave them unstaged and report the scope.

---

### Task 2: Probe Safety Gate and Sanitized Result Model

**Files:**
- Create: `scripts/aurora_capability_canary.py`
- Create: `tests/test_aurora_capability_canary.py`

**Interfaces:**
- Consumes: direct URL `http://127.0.0.1:18080`; gateway URL `http://127.0.0.1:13000`; direct key from `.env.canary`; gateway key from `.secrets/canary/new_api_client_token.txt`.
- Produces: `ProbeError(code: str)`, `HttpResponse`, `CheckResult`, `TargetConfig`, `validate_canary_url()`, `read_env_value()`, `read_single_secret()`, `build_report()` and `serialize_report()`.

- [ ] **Step 1: Write failing tests for the hard safety gate**

Start `tests/test_aurora_capability_canary.py` with the repository's import-by-path pattern, then add:

```python
import base64
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import wave


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "aurora_capability_canary.py"
SPEC = importlib.util.spec_from_file_location("aurora_capability_canary", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class SafetyGateTests(unittest.TestCase):
    def test_only_fixed_loopback_canary_urls_are_accepted(self):
        self.assertEqual(
            MODULE.validate_canary_url("http://127.0.0.1:18080", "direct"),
            "http://127.0.0.1:18080",
        )
        self.assertEqual(
            MODULE.validate_canary_url("http://127.0.0.1:13000", "gateway"),
            "http://127.0.0.1:13000",
        )
        for url in (
            "http://127.0.0.1:8080",
            "http://127.0.0.1:3000",
            "http://192.0.2.10:18080",
            "https://127.0.0.1:18080",
            "https://chatgpt.com",
        ):
            with self.subTest(url=url), self.assertRaises(MODULE.ProbeError):
                MODULE.validate_canary_url(url, "direct")

    def test_missing_real_api_flag_exits_before_transport(self):
        with mock.patch.object(MODULE, "run_matrix") as run:
            with contextlib.redirect_stderr(io.StringIO()):
                exit_code = MODULE.main([])
        self.assertEqual(exit_code, 2)
        run.assert_not_called()

    def test_secret_readers_require_one_nonempty_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env.canary"
            env.write_text("AURORA_CANARY_AUTHORIZATION=service-secret\n", encoding="utf-8")
            token = root / "token.txt"
            token.write_text("gateway-secret\n", encoding="utf-8")
            self.assertEqual(
                MODULE.read_env_value(env, "AURORA_CANARY_AUTHORIZATION"),
                "service-secret",
            )
            self.assertEqual(MODULE.read_single_secret(token), "gateway-secret")
            token.write_text("one\ntwo\n", encoding="utf-8")
            with self.assertRaises(MODULE.ProbeError):
                MODULE.read_single_secret(token)
```

- [ ] **Step 2: Run tests to verify the module is missing**

```powershell
python -m unittest tests.test_aurora_capability_canary.SafetyGateTests -v
```

Expected: import failure for `scripts/aurora_capability_canary.py`.

- [ ] **Step 3: Implement the result model and safety-only CLI path**

Create `scripts/aurora_capability_canary.py` with these public types and constants:

```python
#!/usr/bin/env python3
"""Run an explicitly authorized capability matrix against isolated canary ports."""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import http.client
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave
import zlib
from typing import Callable, Mapping


DIRECT_BASE_URL = "http://127.0.0.1:18080"
GATEWAY_BASE_URL = "http://127.0.0.1:13000"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024
STATUS_ORDER = {"PASS": 0, "FAIL": 1}
EXPECTED_CHECKS = (
    "models",
    "chat_nonstream",
    "chat_stream",
    "responses_nonstream",
    "responses_stream",
    "files",
    "image_generation",
    "image_edit",
    "image_variation",
    "audio_speech",
    "audio_transcription",
    "audio_translation",
)
TRANSPORT_ERROR_CODES = {
    "auth_failed",
    "upstream_forbidden",
    "route_missing",
    "rate_limited",
    "upstream_failed",
    "http_failed",
    "timeout",
    "connectivity_failed",
    "response_too_large",
}
PASS_DETAIL_KEYS = {
    "models_valid": {"count"},
    "chat_nonstream_valid": {"content_present"},
    "chat_stream_valid": {"chunks", "done"},
    "responses_nonstream_valid": {"completed", "output_count"},
    "responses_stream_valid": {"created", "output_seen", "completed", "done"},
    "files_valid": {"upload_accepted", "file_id_present", "answer_present"},
    "image_generation_valid": {"bytes", "media_type", "decodable"},
    "image_edit_valid": {"bytes", "media_type", "decodable"},
    "image_variation_valid": {"bytes", "media_type", "decodable"},
    "audio_speech_valid": {"bytes", "media_type", "decodable"},
    "audio_transcription_valid": {"text_present", "expected_marker_present"},
    "audio_translation_valid": {"text_present", "english_markers_present"},
}
STRUCTURE_ERROR_CODES = {
    "models_invalid",
    "chat_nonstream_invalid",
    "chat_empty",
    "chat_stream_invalid",
    "responses_nonstream_invalid",
    "responses_stream_invalid",
    "files_invalid",
    "image_payload_invalid",
    "image_url_not_accepted",
    "audio_payload_invalid",
    "transcription_mismatch",
    "translation_mismatch",
    "dependency_failed",
}
FAIL_DETAIL_KEYS = {"dependency_failed": {"dependency"}}


class ProbeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    code: str
    details: dict[str, object]

    def __post_init__(self) -> None:
        if self.status not in STATUS_ORDER:
            raise ValueError("invalid status")
        if not self.name.isascii() or not self.code.isascii():
            raise ValueError("invalid result identifier")


@dataclass(frozen=True)
class TargetConfig:
    name: str
    base_url: str
    authorization: str


Transport = Callable[..., HttpResponse]


def validate_canary_url(url: str, target: str) -> str:
    if target not in {"direct", "gateway"}:
        raise ProbeError("unsafe_target")
    expected = DIRECT_BASE_URL if target == "direct" else GATEWAY_BASE_URL
    parsed = urllib.parse.urlsplit(url)
    if url.rstrip("/") != expected or parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ProbeError("unsafe_target")
    return expected
```

Implement the secret readers as exact parsers. They ignore comments/blank lines, accept exactly one value, reject duplicates/empty values/NUL, and never include paths or values in `ProbeError`:

```python
def read_env_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ProbeError("credential_unavailable") from exc
    values = []
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            values.append(stripped[len(prefix):])
    if len(values) != 1 or not values[0] or "\x00" in values[0]:
        raise ProbeError("credential_invalid")
    return values[0]


def read_single_secret(path: Path) -> str:
    try:
        values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError) as exc:
        raise ProbeError("credential_unavailable") from exc
    if len(values) != 1 or "\x00" in values[0]:
        raise ProbeError("credential_invalid")
    return values[0]
```

Add the exact initial parser and `main()` where `--allow-real-api` is required before reading any files or calling `run_matrix()`:

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-real-api", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.allow_real_api:
        print("aurora_canary=ERROR code=real_api_not_authorized", file=sys.stderr)
        return 2
    # Remaining orchestration is added in Task 6.
    return 2
```

Define the Task 2 minimum `run_matrix()` as a side-effect-free function returning an empty target mapping so the safety test can patch it without import errors:

```python
def run_matrix() -> dict[str, list[CheckResult]]:
    return {}
```

Task 6 replaces this minimum body with the tested direct-first orchestration before the CLI can return PASS or perform an authorized request.

- [ ] **Step 4: Run safety tests**

```powershell
python -m unittest tests.test_aurora_capability_canary.SafetyGateTests -v
```

Expected: all safety tests PASS on Windows; importing the module must not require `fcntl`, Docker or network access.

- [ ] **Step 5: Add report allowlist tests and minimal implementation**

Add tests that build two target result maps and assert the serialized report contains only:

```python
class ReportTests(unittest.TestCase):
    def test_report_is_bounded_and_contains_no_secret_or_body(self):
        results = {
            "direct": [MODULE.CheckResult("models", "PASS", "models_valid", {"count": 3})],
            "gateway": [MODULE.CheckResult("models", "FAIL", "auth_failed", {})],
        }
        report = MODULE.build_report(
            results,
            datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        )
        payload = MODULE.serialize_report(report)
        self.assertLessEqual(len(payload), MODULE.MAX_REPORT_BYTES)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(set(report), {"schema_version", "checked_at", "overall", "targets"})
        self.assertNotIn("secret", payload.decode("utf-8"))
        self.assertNotIn("body", payload.decode("utf-8"))
```

Implement `build_report()` using `asdict()` and fixed UTC `Z` timestamps. Implement `serialize_report()` with compact sorted JSON plus newline and the `32 KiB` limit. Reject detail keys outside the per-code allowlist rather than recursively sanitizing arbitrary data.

- [ ] **Step 6: Commit only if authorized**

```powershell
git add -- scripts/aurora_capability_canary.py tests/test_aurora_capability_canary.py
git diff --cached --check
git commit -m "实现 Aurora canary 安全探测框架"
```

---

### Task 3: Bounded HTTP, Chat and Responses Checks

**Files:**
- Modify: `scripts/aurora_capability_canary.py`
- Modify: `tests/test_aurora_capability_canary.py`

**Interfaces:**
- Consumes: `TargetConfig`, `HttpResponse`, `Transport` from Task 2.
- Produces: `http_request()`, `request_json()`, `parse_sse()`, `check_models()`, `check_chat_nonstream()`, `check_chat_stream()`, `check_responses_nonstream()`, `check_responses_stream()`.

- [ ] **Step 1: Write failing transport classification tests**

Use a fake transport returning `HttpResponse`; cover status classes without using body text:

```python
class HttpTests(unittest.TestCase):
    def test_non_2xx_is_classified_without_response_body(self):
        cases = {
            401: "auth_failed",
            403: "upstream_forbidden",
            404: "route_missing",
            429: "rate_limited",
            502: "upstream_failed",
        }
        for status, code in cases.items():
            with self.subTest(status=status):
                response = MODULE.HttpResponse(status, {}, b"raw-private-upstream-body")
                with self.assertRaises(MODULE.ProbeError) as raised:
                    MODULE.require_success(response)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("private", str(raised.exception))

    def test_json_and_sse_are_bounded_and_strict(self):
        payload = MODULE.decode_json(MODULE.HttpResponse(200, {}, b'{"data":[]}'))
        self.assertEqual(payload, {"data": []})
        events = MODULE.parse_sse(
            b"event: response.created\ndata: {\"type\":\"response.created\"}\n\n"
            b"event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n"
            b"data: [DONE]\n\n"
        )
        self.assertEqual([event[0] for event in events], ["response.created", "response.completed", "done"])
```

- [ ] **Step 2: Implement the bounded transport**

Implement `http_request()` with this exact signature and bounded behavior:

```python
ALLOWED_PATHS = {
    "/v1/models",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/files",
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/images/variations",
    "/v1/audio/speech",
    "/v1/audio/transcriptions",
    "/v1/audio/translations",
}


def http_request(
    target: TargetConfig,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 30,
) -> HttpResponse:
    validate_canary_url(target.base_url, target.name)
    if method not in {"GET", "POST"} or path not in ALLOWED_PATHS:
        raise ProbeError("unsafe_request")
    headers = {"Authorization": f"Bearer {target.authorization}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        target.base_url + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ProbeError("response_too_large")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return HttpResponse(response.status, response_headers, payload)
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, {}, b"")
    except TimeoutError as exc:
        raise ProbeError("timeout") from exc
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise ProbeError("connectivity_failed") from exc
```

Additional requirements:

- accepts only validated target base URLs plus a constant endpoint path;
- sets the `Authorization: Bearer <canary-service-key>` header in memory;
- reads at most `MAX_RESPONSE_BYTES + 1` and raises `response_too_large` if exceeded;
- maps `HTTPError.code` without reading or reporting its response body;
- maps timeout/`URLError`/`HTTPException` to fixed `timeout` or `connectivity_failed` codes;
- normalizes response headers to lowercase names;
- never returns the request authorization in any object.

Implement:

```python
def require_success(response: HttpResponse) -> HttpResponse:
    mapping = {
        401: "auth_failed",
        403: "upstream_forbidden",
        404: "route_missing",
        429: "rate_limited",
    }
    if 200 <= response.status < 300:
        return response
    if response.status >= 500:
        raise ProbeError("upstream_failed")
    raise ProbeError(mapping.get(response.status, "http_failed"))
```

`decode_json()` must reject bodies above `MAX_JSON_BYTES`, invalid UTF-8, non-JSON and non-object roots. `parse_sse()` must accept only bounded UTF-8 text, collect `event` plus JSON `data`, recognize `[DONE]`, and never return free-text delta values to the report layer.

- [ ] **Step 3: Write failing model/chat/Responses structure tests**

Use synthetic responses and assert only booleans/counts leave each check. Required cases:

- models list includes `gpt-5-6-pro`, `gpt-5-6-thinking`, `gpt-image-2`;
- chat non-stream has one choice, message object and string content (empty is structurally valid but returns `chat_empty` FAIL for canary capability acceptance);
- chat stream includes at least one chunk and `[DONE]`;
- Responses non-stream has `status=completed` and at least one output item;
- Responses stream contains `response.created`, at least one output event, `response.completed`, then `[DONE]`;
- malformed structures return fixed codes without copying payload data.

Example:

```python
class TextCapabilityTests(unittest.TestCase):
    def test_responses_stream_requires_completed_and_done(self):
        target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")

        def transport(*args, **kwargs):
            return MODULE.HttpResponse(
                200,
                {"content-type": "text/event-stream"},
                b"event: response.created\ndata: {\"type\":\"response.created\"}\n\n"
                b"event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"synthetic\"}\n\n"
                b"event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n"
                b"data: [DONE]\n\n",
            )

        result = MODULE.check_responses_stream(target, transport)
        self.assertEqual((result.status, result.code), ("PASS", "responses_stream_valid"))
        self.assertEqual(result.details, {"created": True, "output_seen": True, "completed": True, "done": True})
```

- [ ] **Step 4: Implement the five text checks**

Use fixed synthetic requests:

```python
CHAT_PROMPT = "Reply with exactly: AURORA-CANARY-OK"
RESPONSES_INPUT = "Reply with exactly: AURORA-CANARY-OK"
```

Do not include these prompts or returned content in `CheckResult.details`. The only allowed details are model count, `content_present`, chunk count, and the four Responses stream booleans shown above.

- [ ] **Step 5: Run focused tests**

```powershell
python -m unittest `
  tests.test_aurora_capability_canary.HttpTests `
  tests.test_aurora_capability_canary.TextCapabilityTests -v
```

Expected: PASS with no network activity.

- [ ] **Step 6: Commit only if authorized**

```powershell
git add -- scripts/aurora_capability_canary.py tests/test_aurora_capability_canary.py
git diff --cached --check
git commit -m "增加 Aurora 文本与 Responses canary 检查"
```

---

### Task 4: Multipart, Files and Image Checks

**Files:**
- Modify: `scripts/aurora_capability_canary.py`
- Modify: `tests/test_aurora_capability_canary.py`

**Interfaces:**
- Consumes: bounded HTTP/JSON interfaces from Task 3.
- Produces: `encode_multipart()`, `make_test_png()`, `decode_image_result()`, `check_files()`, `check_image_generation()`, `check_image_edit()`, `check_image_variation()`.

- [ ] **Step 1: Write failing deterministic media helper tests**

```python
class MultipartAndImageHelperTests(unittest.TestCase):
    def test_test_png_is_deterministic_and_valid(self):
        first = MODULE.make_test_png()
        second = MODULE.make_test_png()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(first), 16 * 1024)

    def test_multipart_contains_names_but_not_local_paths(self):
        content_type, body = MODULE.encode_multipart(
            fields={"model": "gpt-image-2", "prompt": "synthetic edit"},
            files={"image": ("source.png", "image/png", MODULE.make_test_png())},
            boundary="aurora-canary-boundary",
        )
        self.assertEqual(content_type, "multipart/form-data; boundary=aurora-canary-boundary")
        self.assertIn(b'filename="source.png"', body)
        self.assertNotIn(str(Path.cwd()).encode(), body)
```

Implement `make_test_png()` with `struct` and `zlib`: one opaque 64×64 RGB image, no metadata or external file. Implement `encode_multipart()` with CRLF, deterministic field order, ASCII field names, explicit filename and a maximum request size of `8 MiB`.

- [ ] **Step 2: Write failing image response tests**

Cover:

- valid `data[0].b64_json` decodes to PNG/JPEG/WebP signature;
- malformed base64 fails `image_payload_invalid`;
- URL-only result fails `image_url_not_accepted` so signed URLs never enter output or trigger an external fetch;
- generation uses JSON with `response_format=b64_json`;
- edit and variation use multipart with the in-memory PNG;
- result details contain only `bytes`, `media_type`, `decodable`.

```python
def image_json(image: bytes) -> bytes:
    return json.dumps(
        {"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}
    ).encode("utf-8")


class ImageCapabilityTests(unittest.TestCase):
    def test_generation_accepts_decodable_b64_only(self):
        target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")
        result = MODULE.check_image_generation(
            target,
            lambda *args, **kwargs: MODULE.HttpResponse(200, {}, image_json(MODULE.make_test_png())),
        )
        self.assertEqual((result.status, result.code), ("PASS", "image_generation_valid"))
        self.assertEqual(result.details["media_type"], "image/png")
        self.assertNotIn("b64_json", result.details)
```

- [ ] **Step 3: Write failing Files chain tests**

The test transport must observe two requests without storing bodies in the result:

1. multipart upload of filename `aurora-canary.txt`, content `AURORA CANARY SYNTHETIC FILE` and purpose `assistants`;
2. chat request referencing the returned `file_id` and asking for the fixed marker.

Assert the final details are exactly:

```python
{"upload_accepted": True, "file_id_present": True, "answer_present": True}
```

Do not report the actual file id or answer.

- [ ] **Step 4: Implement Files and the three image checks**

Endpoint and model contract:

- `POST /v1/files`, then `POST /v1/chat/completions` with an `input_file` item;
- `POST /v1/images/generations`, model `gpt-image-2`, `response_format=b64_json`, one 1024×1024 image;
- `POST /v1/images/edits`, model `gpt-image-2`, fixed edit prompt, in-memory PNG;
- `POST /v1/images/variations`, model `gpt-image-2`, in-memory PNG and no edit prompt.

If the candidate Aurora implements variation through `/v1/images/edits` rather than `/v1/images/variations`, do not silently alias it in local code. Keep the official compatibility endpoint `/v1/images/variations` in this first probe; a later canary failure will produce `route_missing` and trigger a documented design decision.

- [ ] **Step 5: Run focused tests**

```powershell
python -m unittest `
  tests.test_aurora_capability_canary.MultipartAndImageHelperTests `
  tests.test_aurora_capability_canary.ImageCapabilityTests `
  tests.test_aurora_capability_canary.FileCapabilityTests -v
```

Expected: PASS; no local media file is created.

- [ ] **Step 6: Commit only if authorized**

```powershell
git add -- scripts/aurora_capability_canary.py tests/test_aurora_capability_canary.py
git diff --cached --check
git commit -m "增加 Aurora 文件与图片 canary 检查"
```

---

### Task 5: Audio Speech, Transcription and Translation Checks

**Files:**
- Modify: `scripts/aurora_capability_canary.py`
- Modify: `tests/test_aurora_capability_canary.py`

**Interfaces:**
- Consumes: `encode_multipart()`, bounded transport, `TargetConfig`.
- Produces: `validate_audio()`, `extract_text_result()`, `check_audio_speech()`, `check_audio_transcription()`, `check_audio_translation()`.

- [ ] **Step 1: Write failing audio validation tests**

Use an in-memory WAV fixture generated with `wave`:

```python
def make_wav() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 1600)
    return stream.getvalue()


class AudioHelperTests(unittest.TestCase):
    def test_wav_must_be_decodable_and_bounded(self):
        details = MODULE.validate_audio(make_wav(), "audio/wav")
        self.assertEqual(details["media_type"], "audio/wav")
        self.assertTrue(details["decodable"])
        with self.assertRaises(MODULE.ProbeError):
            MODULE.validate_audio(b"not-audio", "audio/wav")
```

`validate_audio()` must recognize WAV structurally with `wave`; for MP3, OGG/Opus, FLAC, AAC and WebM, require both an allowlisted content type and the corresponding bounded file signature. It must never invoke a player, ffmpeg or external decoder.

- [ ] **Step 2: Write failing three-stage audio chain tests**

The audio chain is intentionally dependent:

1. `POST /v1/audio/speech` with synthetic Chinese text `今天是能力测试。`, model `tts-1`, voice `alloy`, `response_format=wav`;
2. upload the returned audio to `/v1/audio/transcriptions`, model `whisper-1`, language `zh`, and require normalized text to contain `能力测试`;
3. upload the same audio to `/v1/audio/translations`, model `whisper-1`, and require lowercase English text to contain both `capability` and `test`.

Tests must assert the report contains only:

```python
{"bytes": 3244, "media_type": "audio/wav", "decodable": True}
{"text_present": True, "expected_marker_present": True}
{"text_present": True, "english_markers_present": True}
```

The exact byte count in unit tests comes from the test fixture; production code reports the actual bounded count. Neither transcription nor translation text may enter `CheckResult`.

- [ ] **Step 3: Implement the audio checks**

`check_audio_speech()` returns both a `CheckResult` and the in-memory audio bytes to the orchestrator:

```python
def check_audio_speech(
    target: TargetConfig,
    transport: Transport = http_request,
) -> tuple[CheckResult, bytes | None]:
    body = json.dumps(
        {
            "model": "tts-1",
            "input": "今天是能力测试。",
            "voice": "alloy",
            "response_format": "wav",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        response = require_success(
            transport(
                target,
                "POST",
                "/v1/audio/speech",
                body=body,
                content_type="application/json",
                timeout=180,
            )
        )
        details = validate_audio(
            response.body,
            response.headers.get("content-type", ""),
        )
        return CheckResult("audio_speech", "PASS", "audio_speech_valid", details), response.body
    except ProbeError as exc:
        code = exc.code if exc.code in TRANSPORT_ERROR_CODES else "audio_payload_invalid"
        return CheckResult("audio_speech", "FAIL", code, {}), None
```

`check_audio_transcription()` and `check_audio_translation()` accept those bytes. If speech fails, the orchestrator must return fixed dependency failures for both downstream checks and must not send either request.

- [ ] **Step 4: Run focused tests**

```powershell
python -m unittest `
  tests.test_aurora_capability_canary.AudioHelperTests `
  tests.test_aurora_capability_canary.AudioCapabilityTests -v
```

Expected: PASS; no audio is played or written to disk.

- [ ] **Step 5: Commit only if authorized**

```powershell
git add -- scripts/aurora_capability_canary.py tests/test_aurora_capability_canary.py
git diff --cached --check
git commit -m "增加 Aurora 音频 canary 检查"
```

---

### Task 6: Matrix Orchestration, CLI and Report Contract

**Files:**
- Modify: `scripts/aurora_capability_canary.py`
- Modify: `tests/test_aurora_capability_canary.py`
- Create: `docs/contracts/aurora-capability-canary-report-v1.schema.json`

**Interfaces:**
- Consumes: all check functions from Tasks 3–5.
- Produces: `run_target()`, `run_matrix()`, complete CLI, atomic optional `--output`, and JSON report Schema v1.

- [ ] **Step 1: Write failing orchestration tests**

Lock the exact order:

```python
EXPECTED_CHECKS = (
    "models",
    "chat_nonstream",
    "chat_stream",
    "responses_nonstream",
    "responses_stream",
    "files",
    "image_generation",
    "image_edit",
    "image_variation",
    "audio_speech",
    "audio_transcription",
    "audio_translation",
)
```

Test requirements:

- `run_matrix()` always runs direct first; if direct has any FAIL, gateway is marked `direct_dependency_failed` without making gateway requests.
- Gateway runs only when every direct check passes.
- A target PASS requires all 12 checks PASS.
- Missing canary flag exits `2`; a completed matrix with any FAIL exits `1`; full PASS exits `0`.
- `--target direct` runs only direct and is permitted for the first FNOS gate; `--target both` requires direct then gateway; no `gateway`-only option exists.
- `--output` atomically replaces only the requested file and creates no history; a missing/symlink parent fails closed.
- stdout/stderr never contains either loaded secret, response body, prompt, file id, URL or media body.

- [ ] **Step 2: Implement orchestration and exact CLI**

CLI:

```text
--allow-real-api                 required safety acknowledgement
--target {direct,both}           default direct
--env-file PATH                  default .env.canary
--gateway-key-file PATH          default .secrets/canary/new_api_client_token.txt
--output PATH                    optional sanitized JSON report
--json                           render compact JSON to stdout
```

Base URLs are constants and are deliberately not CLI arguments. `--target both` loads the gateway key only after direct PASS. The environment file is read only after the real-API acknowledgement.

Use these exact orchestration signatures:

```python
def run_target(
    target: TargetConfig,
    transport: Transport = http_request,
) -> list[CheckResult]:
    """Run EXPECTED_CHECKS in order; audio children depend on audio_speech."""
    results = [
        check_models(target, transport),
        check_chat_nonstream(target, transport),
        check_chat_stream(target, transport),
        check_responses_nonstream(target, transport),
        check_responses_stream(target, transport),
        check_files(target, transport),
        check_image_generation(target, transport),
        check_image_edit(target, transport),
        check_image_variation(target, transport),
    ]
    speech, audio = check_audio_speech(target, transport)
    results.append(speech)
    if audio is None:
        results.extend(
            [
                CheckResult("audio_transcription", "FAIL", "dependency_failed", {"dependency": "audio_speech"}),
                CheckResult("audio_translation", "FAIL", "dependency_failed", {"dependency": "audio_speech"}),
            ]
        )
    else:
        results.extend(
            [
                check_audio_transcription(target, audio, transport),
                check_audio_translation(target, audio, transport),
            ]
        )
    if tuple(item.name for item in results) != EXPECTED_CHECKS:
        raise ProbeError("invalid_check_order")
    return results


def run_matrix(
    direct: TargetConfig,
    gateway: TargetConfig | None,
    *,
    target_mode: str,
    transport: Transport = http_request,
) -> dict[str, list[CheckResult]]:
    direct_results = run_target(direct, transport)
    results = {"direct": direct_results}
    if target_mode == "direct":
        return results
    if target_mode != "both" or gateway is None:
        raise ProbeError("invalid_target_mode")
    if any(item.status != "PASS" for item in direct_results):
        results["gateway"] = [
            CheckResult(name, "FAIL", "dependency_failed", {"dependency": "direct"})
            for name in EXPECTED_CHECKS
        ]
        return results
    results["gateway"] = run_target(gateway, transport)
    return results
```

In the final `run_target()` body, call the first ten independent/parent checks in `EXPECTED_CHECKS` order, retain audio bytes only in memory, then call transcription and translation or emit `dependency_failed` with `{"dependency": "audio_speech"}`. Assert the returned names equal `EXPECTED_CHECKS` before building a report.

`atomic_write()` must copy the existing `write_n8n_health_status.py` safety pattern conceptually, not import that module: require an existing non-symlink parent, create a temporary file in that directory, `fsync`, `os.replace`, and remove only its own temporary file after failure.

- [ ] **Step 3: Create the fixed JSON Schema document**

Create `docs/contracts/aurora-capability-canary-report-v1.schema.json`. It must:

- set `$schema` to `https://json-schema.org/draft/2020-12/schema`;
- require `schema_version=1`, UTC `checked_at`, `overall`, and `targets`;
- set `additionalProperties: false` at every object level;
- restrict status to `PASS|FAIL` and codes to the fixed enum implemented in the script;
- restrict target names to `direct|gateway` and check names to `EXPECTED_CHECKS`;
- allow detail values only as booleans, non-negative integers or fixed media-type strings;
- contain no example Token, URL, prompt, email or response body.

Add a unit test that loads the schema with `json.load()` and checks the required sets/enums directly. Do not add a JSON Schema validation dependency.

- [ ] **Step 4: Run the complete probe test module**

```powershell
python -m unittest tests.test_aurora_capability_canary -v
```

Expected: all tests PASS without network access.

- [ ] **Step 5: Prove the guard blocks execution locally**

```powershell
python scripts/aurora_capability_canary.py --target direct --json
```

Expected: exit `2`, stderr exactly `aurora_canary=ERROR code=real_api_not_authorized`, no attempt to read `.env.canary`, no network connection and no output file.

- [ ] **Step 6: Commit only if authorized**

```powershell
git add -- scripts/aurora_capability_canary.py tests/test_aurora_capability_canary.py docs/contracts/aurora-capability-canary-report-v1.schema.json
git diff --cached --check
git commit -m "完成 Aurora 多模态 canary 矩阵"
```

---

### Task 7: Candidate Runbook and Full Local Verification

**Files:**
- Create: `docs/aurora_capability_canary.md`
- Modify: `docs/superpowers/specs/2026-08-03-aurora-capability-first-upgrade-design.md`
- Verify only: all files from Tasks 1–6

**Interfaces:**
- Consumes: `docker-compose.canary.yml`, canary CLI and report Schema.
- Produces: an operator-readable candidate runbook and a fully verified local change set; no FNOS state.

- [ ] **Step 1: Write the candidate runbook with exact gates**

Create `docs/aurora_capability_canary.md` with these sections and facts:

1. **状态**：候选、未部署；不代表 Aurora 2.5、自动续期或多模态能力已通过。
2. **安全边界**：生产 Compose/容器/SQLite/cron/n8n 不变；canary 仅使用 loopback `18080/13000`；Aurora canary 额外挂接生产内部网络只为访问 `mihomo:7890`，这不是网络硬隔离。
3. **FNOS 前置只读核对**：架构、外部网络名、端口占用、容器 UID/GID、secret 文件元数据和当前生产状态；不读取 secret 正文。
4. **候选镜像门禁**：核对 Aurora 官方来源、VERSION、MIT license、linux/amd64 manifest 和不可变 digest；拉取另行批准。
5. **secret 门禁**：只挂载 `.secrets/canary/session_tokens.txt`；非 world-readable；容器内只读；不能通过放宽到 `644` 解决权限。
6. **New API canary 初始化**：独立空数据库，渠道 base URL 精确为 `http://aurora-canary:8080`，渠道 key 为稳定服务 key；客户端 token 只保存在 `.secrets/canary/new_api_client_token.txt`。
7. **真实执行顺序**：先 `--target direct`，全部 PASS 后才运行 `--target both`；每次真实调用都需独立批准。
8. **远端副作用**：Files/图片/音频会在 ChatGPT 上游产生最小合成请求；若上游没有删除接口，测试制品不能主动清理，必须在授权前披露。
9. **自然续期门禁**：保持 canary 到旧 access token 自然过期后复测，再单独重启 canary Aurora 复测；当前工具的能力 PASS 不等于续期 PASS。
10. **停止与清理**：失败时生产不变；是否保留或删除 canary 容器、数据、状态报告和镜像由独立清理授权决定。

Include commands only as future approved examples, always using:

```bash
docker compose --env-file .env.canary -f docker-compose.canary.yml --profile canary config
python3 scripts/aurora_capability_canary.py --allow-real-api --target direct --json
python3 scripts/aurora_capability_canary.py --allow-real-api --target both --output data/canary/evidence/latest-capability.json --json
```

Mark `pull`, `up`, real probe execution, restart and cleanup as not authorized by this local plan.

- [ ] **Step 2: Link the approved design to this plan and contract**

Add a short “实施制品” paragraph to the design spec with relative links to:

- `../plans/2026-08-03-aurora-capability-canary-local-preparation.md`
- `../../contracts/aurora-capability-canary-report-v1.schema.json`
- `../../aurora_capability_canary.md`

State explicitly that health Schema v2 is not part of this plan because canary has not established a stable first-party renewal signal. This is a scoped deferral, not an unfilled placeholder.

- [ ] **Step 3: Run focused and full regression tests**

Run on Windows:

```powershell
python -m unittest tests.test_canary_compose_contract -v
python -m unittest tests.test_aurora_capability_canary -v
python -m unittest discover -s tests -v
```

Expected: all tests PASS. Existing platform-specific behavior remains as documented; if Windows cannot import a POSIX-only existing test, run the full existing suite in WSL and report Windows/WSL results separately rather than skipping tests.

- [ ] **Step 4: Compile tracked Python entrypoints in memory without leaving cache**

Run on Windows; `compile()` parses bytecode in memory and does not import POSIX-only modules or create `.pyc` files:

```powershell
python -B -c "from pathlib import Path; paths=[Path('scripts/aurora_capability_canary.py'),Path('scripts/check_stack_health.py'),Path('scripts/refresh_chatgpt_access_token.py'),Path('scripts/write_n8n_health_status.py')]; [compile(p.read_text(encoding='utf-8'),str(p),'exec') for p in paths]"
```

Expected: exit `0`; this command creates no `__pycache__` directory.

- [ ] **Step 5: Validate Compose, links, diff and ignore boundaries**

Run:

```powershell
git diff --check
git status --short
git check-ignore -v -- .env .env.canary .secrets data artifacts/example.fpk TODO.md DEVLOG.md
git ls-files -- .env .env.canary .secrets data artifacts TODO.md DEVLOG.md
```

Expected:

- `git diff --check` passes;
- all sensitive/runtime/local-maintenance paths are ignored;
- `git ls-files` returns no sensitive/runtime/local-maintenance file;
- status contains only the plan-approved files;
- production `docker-compose.yml`, `DESIGN.md`, `README.md`, `docs/fnos_deployment.md`, refresh script and health producer have no diff.

Validate the modified Markdown relative links with this exact PowerShell check:

```powershell
$markdown = @(
  'docs/aurora_capability_canary.md',
  'docs/superpowers/specs/2026-08-03-aurora-capability-first-upgrade-design.md',
  'docs/superpowers/plans/2026-08-03-aurora-capability-canary-local-preparation.md'
)
$missing = @()
foreach ($file in $markdown) {
  $text = Get-Content -Raw -LiteralPath $file
  foreach ($match in [regex]::Matches($text, '\[[^\]]+\]\(([^)]+)\)')) {
    $target = $match.Groups[1].Value
    if ($target -notmatch '^(https?://|#)') {
      $path = Join-Path (Split-Path -Parent $file) $target
      if (-not (Test-Path -LiteralPath $path)) { $missing += "$file -> $target" }
    }
  }
}
if ($missing.Count) { $missing; exit 1 }
```

Expected: exit `0` with no missing link. Validate canary Compose with the non-sensitive static `docker compose config` command from Task 1; do not run `pull`, `create`, `up`, `start` or `run`.

- [ ] **Step 6: Review for sensitive literals**

Search tracked candidate files only:

```powershell
rg -n --hidden -g '!data/**' -g '!.secrets/**' -g '!.env*' `
  'eyJ[A-Za-z0-9_-]{20,}|__Secure-next-auth\.session-token=|AURORA_CANARY_AUTHORIZATION=.+|NEW_API_CANARY_SESSION_SECRET=.+' `
  docker-compose.canary.yml scripts tests docs .env.canary.example
```

Expected: no real secret-like match. Variable references and empty example assignments are allowed; any nonempty secret assignment blocks completion.

- [ ] **Step 7: Audit the cumulative branch scope, then commit only Task 7 residual files**

First audit the complete branch range from the approved baseline:

```powershell
git diff --name-only 30b8bd4..HEAD
git status --short
```

Expected cumulative branch paths are limited to the ten plan-approved files. Files already committed by Tasks 1–6 will not re-enter the index. Stage and commit only the Task 7 residual documentation:

```powershell
git add -- `
  docs/contracts/aurora-capability-canary-report-v1.schema.json `
  docs/aurora_capability_canary.md `
  docs/superpowers/specs/2026-08-03-aurora-capability-first-upgrade-design.md `
  docs/superpowers/plans/2026-08-03-aurora-capability-canary-local-preparation.md
git diff --cached --name-only
git diff --cached --check
git commit -m "准备 Aurora 多模态能力 canary"
```

Expected: staged names contain only Task 7 files that still differ from `HEAD`; the cumulative `30b8bd4..HEAD` range plus this commit contains exactly the ten approved files. Do not squash or push without separate explicit authorization.

## Completion Gate

This local preparation plan is complete only when:

- the canary Compose contract and all capability probe tests pass;
- the real-API guard demonstrably blocks local execution without the flag;
- the full existing test suite passes in its supported Windows/WSL environment;
- Compose static parsing, Markdown links, diff and ignore checks pass;
- no secret-like literal, generated media, report history or runtime data is tracked;
- production Compose, scripts, cron, n8n, database and FNOS remain unchanged;
- the handoff explicitly states that no Aurora 2.5 capability, automatic renewal or production readiness has yet been proven.

After this plan, the next separately designed/approved unit is the FNOS read-only baseline plus isolated canary creation plan. The health `latest.json` Schema v2 plan must wait until the canary identifies a stable, non-sensitive renewal signal; it must not infer internal Token health from free-text logs.
