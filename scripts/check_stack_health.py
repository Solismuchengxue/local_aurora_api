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


def overall_status(results: list[CheckResult]) -> str:
    if not results:
        return "FAIL"
    return max(results, key=lambda item: STATUS_ORDER[item.status]).status


def _sanitize_value(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        return safe_text(value, secrets)
    if isinstance(value, dict):
        return {
            _sanitize_value(key, secrets): _sanitize_value(item, secrets)
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
