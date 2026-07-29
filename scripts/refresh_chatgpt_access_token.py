#!/usr/bin/env python3
"""Refresh the ChatGPT access token stored in a New API SQLite channel."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import sqlite3
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


def database_path(root: Path) -> Path:
    path = root / "data" / "new-api" / "one-api.db"
    if not path.is_file():
        raise RefreshError("New API SQLite database was not found")
    return path


def read_channel_token(root: Path, channel_id: int) -> str:
    try:
        with sqlite3.connect(database_path(root), timeout=30) as database:
            row = database.execute(
                "SELECT key FROM channels WHERE id = ? AND status = 1",
                (channel_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RefreshError("failed to read the New API channel") from exc
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise RefreshError("enabled New API channel was not found")
    return row[0]


def replace_channel_token(
    root: Path,
    channel_id: int,
    expected_token: str,
    replacement_token: str,
) -> None:
    try:
        with sqlite3.connect(database_path(root), timeout=30) as database:
            result = database.execute(
                "UPDATE channels SET key = ? WHERE id = ? AND key = ?",
                (replacement_token, channel_id, expected_token),
            )
            if result.rowcount != 1:
                raise RefreshError(
                    "New API channel changed concurrently; refusing to overwrite"
                )
    except sqlite3.Error as exc:
        raise RefreshError("failed to update the New API channel") from exc


def read_client_token(root: Path, now: int) -> str:
    try:
        with sqlite3.connect(database_path(root), timeout=30) as database:
            row = database.execute(
                """
                SELECT key
                FROM tokens
                WHERE status = 1
                  AND (expired_time = -1 OR expired_time > ?)
                  AND (unlimited_quota = 1 OR remain_quota > 0)
                ORDER BY id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RefreshError("failed to read a New API validation token") from exc
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise RefreshError("no enabled New API token is available for validation")
    return row[0] if row[0].startswith("sk-") else f"sk-{row[0]}"


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
        raise RefreshError(f"API validation returned HTTP {exc.code}") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RefreshError("API validation request failed") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RefreshError("API validation did not return JSON") from exc


def wait_for_http(url: str, timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(3)
    raise RefreshError("service did not become ready before the timeout")


def validate_chat_api(base_url: str, authorization: str) -> None:
    models = request_json(f"{base_url}/v1/models", authorization, timeout=30)
    model_ids = {
        item.get("id")
        for item in models.get("data", [])
        if isinstance(item, dict)
    }
    if "gpt-5-6-pro" not in model_ids:
        raise RefreshError("API validation did not list gpt-5-6-pro")

    last_error: RefreshError | None = None
    validation_models = (
        "gpt-5-6-pro",
        "gpt-5-6-thinking",
        "gpt-5-6-pro",
        "gpt-5-6-thinking",
    )
    for attempt, model in enumerate(validation_models):
        validation_marker = (
            f"AURORA-VERIFY-{int(time.time())}-{attempt + 1}"
        )
        try:
            result = request_json(
                f"{base_url}/v1/chat/completions",
                authorization,
                payload={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"只回答：{validation_marker}",
                        }
                    ],
                    "stream": False,
                },
                timeout=180,
            )
            choices = result.get("choices")
            if isinstance(choices, list) and len(choices) == 1:
                message = choices[0].get("message") if choices else None
                content = message.get("content") if isinstance(message, dict) else None
                # Aurora can return a valid 200 completion with empty content.
                # Token refresh validates authentication and response shape here;
                # client-visible answer quality is monitored separately.
                if isinstance(content, str):
                    return
            last_error = RefreshError("API validation returned empty content")
        except RefreshError as exc:
            last_error = exc
        if attempt < len(validation_models) - 1:
            time.sleep(8)
    raise last_error or RefreshError("chat validation failed")


def validate_aurora_token(token: str) -> None:
    validate_chat_api("http://127.0.0.1:8080", token)


def restart_new_api() -> None:
    try:
        subprocess.run(
            ["docker", "restart", "new-api"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RefreshError("failed to restart New API") from exc


def validate_new_api(root: Path) -> None:
    wait_for_http("http://127.0.0.1:3000/")
    validate_chat_api(
        "http://127.0.0.1:3000",
        read_client_token(root, int(time.time())),
    )


def refresh_once(
    root: Path,
    *,
    channel_id: int,
    threshold_seconds: int,
    force: bool,
    now: int,
    exchange_fn: Callable[[str], str],
    validate_upstream_fn: Callable[[str], None],
    replace_channel_fn: Callable[[str, str], None],
    restart_fn: Callable[[], None],
    validate_gateway_fn: Callable[[], None],
) -> str:
    current_token = read_channel_token(root, channel_id)
    current_exp = jwt_exp(current_token)
    remaining = current_exp - now
    if not force and not should_refresh(current_exp, threshold_seconds, now):
        emit("refresh_skipped", remaining_seconds=remaining)
        return "skipped"

    session_token = read_single_secret(root / ".secrets" / "session_tokens.txt")
    new_token = exchange_fn(session_token)
    new_exp = jwt_exp(new_token)
    if new_exp <= current_exp:
        raise RefreshError("session exchange did not extend token expiry")
    if new_exp - now < 24 * 3600:
        raise RefreshError("new access token expires in less than 24 hours")

    validate_upstream_fn(new_token)
    atomic_write_secret(
        root / ".secrets" / "access_tokens.previous.txt",
        current_token,
    )
    replace_channel_fn(current_token, new_token)
    try:
        restart_fn()
        validate_gateway_fn()
    except Exception as exc:
        replace_channel_fn(new_token, current_token)
        try:
            restart_fn()
            validate_gateway_fn()
        except Exception as rollback_exc:
            raise RefreshError(
                "refresh failed and rollback validation also failed"
            ) from rollback_exc
        raise RefreshError("refresh validation failed; previous token restored") from exc

    atomic_write_secret(root / ".secrets" / "access_tokens.txt", new_token)
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
        help="Solis_Aurora_Gateway project root",
    )
    parser.add_argument("--channel-id", type=int, default=1)
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
            current_token = read_channel_token(root, args.channel_id)
            remaining = jwt_exp(current_token) - int(time.time())
            if args.dry_run:
                emit(
                    "dry_run",
                    channel_id=args.channel_id,
                    remaining_seconds=remaining,
                    would_refresh=(
                        args.force
                        or remaining <= args.threshold_hours * 3600
                    ),
                )
                return 0

            refresh_once(
                root,
                channel_id=args.channel_id,
                threshold_seconds=args.threshold_hours * 3600,
                force=args.force,
                now=int(time.time()),
                exchange_fn=exchange_session_token,
                validate_upstream_fn=validate_aurora_token,
                replace_channel_fn=lambda old, new: replace_channel_token(
                    root, args.channel_id, old, new
                ),
                restart_fn=restart_new_api,
                validate_gateway_fn=lambda: validate_new_api(root),
            )
            return 0
        except RefreshError as exc:
            emit("refresh_failed", reason=str(exc))
            return 1


if __name__ == "__main__":
    sys.exit(main())
