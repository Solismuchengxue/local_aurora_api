#!/usr/bin/env python3
"""Refresh Aurora's ChatGPT access-token pool from a session token."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Callable
import urllib.error
import urllib.request


class RefreshError(RuntimeError):
    """A safe-to-log token refresh failure."""


def emit(event: str, **fields: object) -> None:
    record = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


def read_single_secret(path: Path) -> str:
    if not path.is_file():
        raise RefreshError(f"missing secret file: {path.name}")
    values = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(values) != 1:
        raise RefreshError(f"{path.name} must contain exactly one non-empty line")
    return values[0]


def read_env_value(path: Path, name: str) -> str:
    if not path.is_file():
        raise RefreshError(f"missing environment file: {path.name}")
    prefix = f"{name}="
    values = [
        line[len(prefix) :].strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.startswith(prefix)
    ]
    if len(values) != 1 or not values[0]:
        raise RefreshError(f"{name} must be set exactly once in {path.name}")
    return values[0]


def jwt_exp(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        result = json.loads(base64.urlsafe_b64decode(payload))
        return int(result["exp"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RefreshError("access token is not a JWT with a valid exp claim") from exc


def should_refresh(exp: int, threshold_seconds: int, now: int) -> bool:
    return exp - now <= threshold_seconds


def discover_mihomo_proxy(container: str = "mihomo") -> str:
    try:
        address = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                container,
            ],
            text=True,
            timeout=15,
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RefreshError("failed to inspect the Mihomo container") from exc
    if not address or any(char not in "0123456789." for char in address):
        raise RefreshError("Mihomo container did not expose a valid IPv4 address")
    return f"http://{address}:7890"


def exchange_session_token(session_token: str, timeout: int = 90) -> str:
    proxy_url = discover_mihomo_proxy()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    request = urllib.request.Request(
        "https://chatgpt.com/api/auth/session",
        headers={
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": f"__Secure-next-auth.session-token={session_token}",
            "Oai-Language": "en-US",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise RefreshError(f"session exchange returned HTTP {exc.code}") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RefreshError("session exchange request failed") from exc
    if status != 200:
        raise RefreshError(f"session exchange returned HTTP {status}")
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RefreshError("session exchange did not return JSON") from exc
    access_token = result.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise RefreshError("session exchange returned no access token")
    return access_token


def atomic_write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def replace_with_backup(path: Path, backup_path: Path, value: str) -> str:
    previous = read_single_secret(path)
    atomic_write_secret(backup_path, previous)
    atomic_write_secret(path, value)
    return previous


def request_json(
    url: str,
    authorization: str,
    *,
    payload: dict[str, object] | None = None,
    timeout: int = 30,
) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {authorization}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise RefreshError(f"Aurora validation returned HTTP {exc.code}") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RefreshError("Aurora validation request failed") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RefreshError("Aurora validation did not return JSON") from exc


def wait_for_aurora(timeout: int = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8080/", timeout=5
            ) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(4)
    raise RefreshError("Aurora did not become ready before the timeout")


def restart_aurora(root: Path) -> None:
    base = root / "docker-compose.yml"
    override = root / ".secrets" / "compose.scheduled-refresh.yaml"
    if not override.is_file():
        raise RefreshError("missing compose.scheduled-refresh.yaml")
    try:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "aurora-stack",
                "-f",
                str(base),
                "-f",
                str(override),
                "up",
                "-d",
                "--force-recreate",
                "aurora",
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RefreshError("failed to recreate Aurora") from exc


def validate_aurora(root: Path) -> None:
    wait_for_aurora()
    authorization = read_env_value(root / ".env", "AURORA_AUTHORIZATION")
    models = request_json(
        "http://127.0.0.1:8080/v1/models", authorization, timeout=30
    )
    model_ids = {
        item.get("id")
        for item in models.get("data", [])
        if isinstance(item, dict)
    }
    if "gpt-5-6-pro" not in model_ids:
        raise RefreshError("Aurora validation did not list gpt-5-6-pro")

    last_error: RefreshError | None = None
    for attempt in range(3):
        try:
            result = request_json(
                "http://127.0.0.1:8080/v1/chat/completions",
                authorization,
                payload={
                    "model": "gpt-5-6-pro",
                    "messages": [{"role": "user", "content": "只回答：OK"}],
                    "stream": False,
                },
                timeout=180,
            )
            choices = result.get("choices")
            if isinstance(choices, list) and len(choices) == 1:
                message = choices[0].get("message") if choices else None
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str) and content:
                    return
            last_error = RefreshError("Aurora validation returned empty content")
        except RefreshError as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(8)
    raise last_error or RefreshError("Aurora chat validation failed")


def refresh_once(
    root: Path,
    *,
    threshold_seconds: int,
    force: bool,
    now: int,
    exchange_fn: Callable[[str], str],
    restart_fn: Callable[[], None],
    validate_fn: Callable[[], None],
) -> str:
    secrets = root / ".secrets"
    access_path = secrets / "access_tokens.txt"
    session_path = secrets / "session_tokens.txt"
    backup_path = secrets / "access_tokens.previous.txt"

    current_token = read_single_secret(access_path)
    current_exp = jwt_exp(current_token)
    remaining = current_exp - now
    if not force and not should_refresh(current_exp, threshold_seconds, now):
        emit("refresh_skipped", remaining_seconds=remaining)
        return "skipped"

    session_token = read_single_secret(session_path)
    new_token = exchange_fn(session_token)
    new_exp = jwt_exp(new_token)
    if new_exp <= current_exp:
        raise RefreshError("session exchange did not extend token expiry")
    if new_exp - now < 24 * 3600:
        raise RefreshError("new access token expires in less than 24 hours")

    previous = replace_with_backup(access_path, backup_path, new_token)
    try:
        restart_fn()
        validate_fn()
    except Exception as exc:
        atomic_write_secret(access_path, previous)
        try:
            restart_fn()
            validate_fn()
        except Exception as rollback_exc:
            raise RefreshError(
                "refresh failed and rollback validation also failed"
            ) from rollback_exc
        raise RefreshError("refresh validation failed; previous token restored") from exc

    emit(
        "refresh_succeeded",
        previous_exp=current_exp,
        new_exp=new_exp,
        extension_seconds=new_exp - current_exp,
    )
    return "refreshed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="local_aurora_api project root",
    )
    parser.add_argument("--threshold-hours", type=int, default=72)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    lock_path = root / ".secrets" / "refresh_chatgpt_access_token.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit("refresh_skipped", reason="another refresh is running")
            return 0

        try:
            access_token = read_single_secret(
                root / ".secrets" / "access_tokens.txt"
            )
            remaining = jwt_exp(access_token) - int(time.time())
            if args.dry_run:
                emit(
                    "dry_run",
                    remaining_seconds=remaining,
                    would_refresh=(
                        args.force
                        or remaining <= args.threshold_hours * 3600
                    ),
                )
                return 0

            refresh_once(
                root,
                threshold_seconds=args.threshold_hours * 3600,
                force=args.force,
                now=int(time.time()),
                exchange_fn=exchange_session_token,
                restart_fn=lambda: restart_aurora(root),
                validate_fn=lambda: validate_aurora(root),
            )
            return 0
        except RefreshError as exc:
            emit("refresh_failed", reason=str(exc))
            return 1


if __name__ == "__main__":
    sys.exit(main())
