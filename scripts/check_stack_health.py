#!/usr/bin/env python3
"""Read-only health check for the local_aurora_api fnOS stack."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Callable
import urllib.error
import urllib.request

import refresh_chatgpt_access_token as token_refresh


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
EXPECTED_CONTAINERS = ("aurora", "new-api", "mihomo", "metacubexd")
KNOWN_REFRESH_EVENTS = {
    "refresh_skipped",
    "refresh_succeeded",
    "refresh_failed",
}
LOG_INTEGER_FIELDS = {
    "remaining_seconds",
    "channel_id",
    "previous_exp",
    "new_exp",
    "extension_seconds",
}
EXPECTED_MODELS = {"gpt-5-6-pro", "gpt-5-6-thinking"}
NEW_API_BASE = "http://127.0.0.1:3000"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    summary: str
    details: dict[str, object]

    def __post_init__(self) -> None:
        if self.status not in STATUS_ORDER:
            raise ValueError("unknown check status")


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
        if not isinstance(inspected, list):
            raise ValueError("docker inspect output was not a list")
        states = {}
        for item in inspected:
            if not isinstance(item, dict):
                raise ValueError("docker inspect item was not an object")
            raw_name = item.get("Name")
            if not isinstance(raw_name, str):
                raise ValueError("docker inspect name was invalid")
            name = raw_name.lstrip("/")
            if name not in EXPECTED_CONTAINERS:
                continue
            state = item.get("State")
            restart_count = item.get("RestartCount")
            if (
                not isinstance(state, dict)
                or not isinstance(state.get("Status"), str)
                or not isinstance(restart_count, int)
                or isinstance(restart_count, bool)
            ):
                raise ValueError("docker inspect state was invalid")
            states[name] = {
                "state": state["Status"],
                "restart_count": restart_count,
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
            {
                "running": sum(
                    value["state"] == "running"
                    for value in states.values()
                ),
                "containers": states,
            },
        )
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        ValueError,
    ):
        return CheckResult(
            "containers",
            "FAIL",
            "Docker 状态检查失败",
            {"error": "docker_inspect_error"},
        )


def check_database(
    root: Path,
    channel_id: int,
    now: int,
) -> tuple[CheckResult, str | None, tuple[str, ...]]:
    path = root / "data" / "new-api" / "one-api.db"
    channel_token = ""
    raw_client_token = ""
    client_token = ""
    try:
        with sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=30,
        ) as database:
            integrity = database.execute("PRAGMA integrity_check").fetchone()
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
        raw_client_token = str(client[0])
        client_token = (
            raw_client_token
            if raw_client_token.startswith("sk-")
            else f"sk-{raw_client_token}"
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
            tuple(
                dict.fromkeys(
                    value
                    for value in (
                        channel_token,
                        raw_client_token,
                        client_token,
                    )
                    if value
                )
            ),
        )
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        token_refresh.RefreshError,
    ):
        secrets = tuple(
            value
            for value in (channel_token, raw_client_token, client_token)
            if value
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
            if not isinstance(raw, dict):
                invalid_lines += 1
                continue
            event = raw.get("event")
            record = {
                "event": event
                if isinstance(event, str) and event in KNOWN_REFRESH_EVENTS
                else "unknown"
            }
            timestamp = raw.get("time")
            if (
                isinstance(timestamp, str)
                and 0 < len(timestamp) <= 64
                and "T" in timestamp
            ):
                try:
                    if datetime.fromisoformat(timestamp).tzinfo is not None:
                        record["time"] = timestamp
                except ValueError:
                    pass
            for key in LOG_INTEGER_FIELDS:
                value = raw.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    record[key] = value
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
    event = latest["event"]
    if event == "refresh_failed":
        status = "FAIL"
    elif event in {"refresh_skipped", "refresh_succeeded"}:
        status = "WARN" if invalid_lines else "PASS"
    else:
        status = "FAIL"
    summary = "未知续期事件" if event == "unknown" else f"最新事件为 {event}"
    return CheckResult(
        "refresh_log",
        status,
        summary,
        {**latest, "invalid_lines": invalid_lines},
    )


def overall_status(results: list[CheckResult]) -> str:
    if not results:
        return "FAIL"
    return max(results, key=lambda item: STATUS_ORDER[item.status]).status


def _sanitize_value(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        return safe_text(value, secrets)
    if isinstance(value, dict):
        return {
            key: _sanitize_value(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item, secrets) for item in value)
    return value


def build_report(
    results: list[CheckResult],
    checked_at: str,
    secrets: tuple[str, ...] = (),
) -> dict[str, object]:
    checks = []
    for result in results:
        check = asdict(result)
        if secrets:
            check["summary"] = safe_text(check["summary"], secrets)
            check["details"] = _sanitize_value(check["details"], secrets)
        checks.append(check)
    return {
        "checked_at": checked_at,
        "overall": overall_status(results),
        "checks": checks,
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
