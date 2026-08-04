#!/usr/bin/env python3
"""Publish a bounded, sanitized Aurora health document for offline consumers."""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import base64
import hmac
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Sequence

from aurora_session_renewal_probe import read_env_value

try:
    from refresh_chatgpt_access_token import RefreshError, jwt_exp
except ModuleNotFoundError as import_error:
    if import_error.name != "fcntl":
        raise

    class RefreshError(RuntimeError):
        """Safe token parsing failure for non-POSIX local verification."""

    def jwt_exp(token: str) -> int:
        """Parse a JWT exp claim when the POSIX refresh module cannot load."""
        try:
            encoded = token.split(".")[1]
            encoded += "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded))
            return int(payload["exp"])
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RefreshError("invalid JWT expiry") from exc


STATUS_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}
EXPECTED_CHECKS = (
    "containers",
    "runtime_contract",
    "local_tcp",
    "database",
    "refresh_log",
)
ALLOWED_CODES = {
    "containers": {
        "containers_running",
        "container_state_invalid",
        "container_inspect_failed",
    },
    "runtime_contract": {
        "runtime_matches",
        "runtime_mismatch",
        "runtime_inspect_failed",
    },
    "local_tcp": {
        "local_ports_reachable",
        "local_port_unreachable",
        "tcp_target_unavailable",
    },
    "database": {
        "database_and_token_valid",
        "database_and_service_key_valid",
        "token_near_expiry",
        "token_expired",
        "database_invalid",
    },
    "refresh_log": {
        "refresh_recent",
        "refresh_not_applicable",
        "refresh_missing",
        "refresh_stale",
        "refresh_malformed",
        "refresh_failed",
        "refresh_in_progress_overrun",
        "refresh_lock_unavailable",
    },
}
EXPECTED_STATUS_BY_CODE = {
    "containers_running": "PASS",
    "container_state_invalid": "FAIL",
    "container_inspect_failed": "FAIL",
    "runtime_matches": "PASS",
    "runtime_mismatch": "FAIL",
    "runtime_inspect_failed": "FAIL",
    "local_ports_reachable": "PASS",
    "local_port_unreachable": "FAIL",
    "tcp_target_unavailable": "FAIL",
    "database_and_token_valid": "PASS",
    "database_and_service_key_valid": "PASS",
    "token_near_expiry": "WARN",
    "token_expired": "FAIL",
    "database_invalid": "FAIL",
    "refresh_recent": "PASS",
    "refresh_not_applicable": "PASS",
    "refresh_missing": "WARN",
    "refresh_stale": "WARN",
    "refresh_malformed": "WARN",
    "refresh_failed": "FAIL",
    "refresh_in_progress_overrun": "FAIL",
    "refresh_lock_unavailable": "FAIL",
}
SCHEMA_VERSION = 1
PRODUCER = "Solis_Aurora_Gateway"
MAX_STATUS_BYTES = 16 * 1024
EXPECTED_CONTAINERS = ("aurora", "new-api", "mihomo", "metacubexd")
EXPECTED_ROOT = "/vol1/1000/Solis_Aurora_Gateway"
SESSION_CANARY_BASE_URL = "http://aurora-session-renewal-canary:8080"
SERVICE_KEY_DETAIL_KEYS = frozenset(
    {
        "integrity_ok",
        "channel_active",
        "channel_base_matches",
        "service_key_matches",
    }
)

CommandRunner = Callable[[list[str], int], subprocess.CompletedProcess[str]]
TcpConnector = Callable[[tuple[str, int], float], object]
LockProbe = Callable[[Path], bool]


@dataclass(frozen=True)
class CheckResult:
    status: str
    code: str
    details: dict[str, object]

    def __post_init__(self) -> None:
        if self.status not in STATUS_ORDER:
            raise ValueError("invalid status")
        if not self.code or not self.code.isascii():
            raise ValueError("invalid code")


@dataclass(frozen=True)
class MountSnapshot:
    destination: str
    source: str
    read_write: bool


@dataclass(frozen=True)
class ContainerSnapshot:
    name: str
    state: str
    restart_count: int
    project: str
    working_dir: str
    mounts: Sequence[MountSnapshot]


def overall_status(results: dict[str, CheckResult]) -> str:
    if not results:
        raise ValueError("missing check results")
    return max((result.status for result in results.values()), key=STATUS_ORDER.get)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _valid_utc_text(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo == timezone.utc and parsed.microsecond == 0


def _valid_nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _validate_details(
    name: str, code: str, details: dict[str, object]
) -> None:
    if type(details) is not dict:
        raise ValueError("invalid check details")
    if name == "containers":
        if set(details) != {"expected", "running", "restart_counts"}:
            raise ValueError("invalid check details")
        counts = details["restart_counts"]
        valid = (
            details["expected"] == 4
            and _valid_nonnegative_integer(details["running"])
            and details["running"] <= 4
            and type(counts) is dict
            and set(counts).issubset(EXPECTED_CONTAINERS)
            and all(_valid_nonnegative_integer(value) for value in counts.values())
        )
    elif name == "runtime_contract":
        if set(details) != {
            "project_matches",
            "working_dir_matches",
            "mounts_match",
        }:
            raise ValueError("invalid check details")
        valid = all(type(value) is bool for value in details.values())
    elif name == "local_tcp":
        if set(details) != {"expected", "reachable", "services"}:
            raise ValueError("invalid check details")
        services = details["services"]
        valid = (
            details["expected"] == 4
            and _valid_nonnegative_integer(details["reachable"])
            and details["reachable"] <= 4
            and type(services) is dict
            and set(services) == set(PORT_SPECS)
            and all(type(value) is bool for value in services.values())
        )
    elif name == "database":
        service_keys = SERVICE_KEY_DETAIL_KEYS
        if code == "database_and_service_key_valid":
            valid = (
                set(details) == service_keys
                and all(type(value) is bool for value in details.values())
                and all(details.values())
            )
        elif code == "database_invalid" and set(details) == service_keys:
            valid = (
                all(type(value) is bool for value in details.values())
                and not all(details.values())
            )
        else:
            if set(details) != {
                "integrity_ok",
                "remaining_seconds",
                "expires_at",
            }:
                raise ValueError("invalid check details")
            valid = (
                type(details["integrity_ok"]) is bool
                and type(details["remaining_seconds"]) is int
                and _valid_utc_text(details["expires_at"])
            )
    elif name == "refresh_log":
        if set(details) != {
            "event",
            "valid_records",
            "invalid_records",
            "event_at",
        }:
            raise ValueError("invalid check details")
        if code == "refresh_not_applicable":
            valid = (
                details["event"] == "not_applicable"
                and details["valid_records"] == 0
                and details["invalid_records"] == 0
                and _valid_utc_text(details["event_at"])
            )
        else:
            valid = (
                details["event"]
                in {
                    "refresh_skipped",
                    "refresh_succeeded",
                    "refresh_failed",
                    "refresh_in_progress",
                    "missing",
                    "lock_unavailable",
                    "invalid_file",
                    "oversized",
                    "unreadable",
                    "invalid_time",
                }
                and _valid_nonnegative_integer(details["valid_records"])
                and _valid_nonnegative_integer(details["invalid_records"])
                and _valid_utc_text(details["event_at"])
            )
    else:
        valid = False
    if not valid:
        raise ValueError("invalid check details")


def build_document(
    results: dict[str, CheckResult], generated_at: datetime
) -> dict[str, object]:
    if tuple(results) != EXPECTED_CHECKS:
        raise ValueError("invalid check set or order")
    for name, result in results.items():
        if result.code not in ALLOWED_CODES[name]:
            raise ValueError("invalid check code")
        if result.status != EXPECTED_STATUS_BY_CODE[result.code]:
            raise ValueError("invalid check status")
        _validate_details(name, result.code, result.details)
    return {
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "generated_at": _utc_text(generated_at),
        "overall": overall_status(results),
        "checks": {name: asdict(result) for name, result in results.items()},
    }


def serialize_document(document: dict[str, object]) -> bytes:
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_STATUS_BYTES:
        raise ValueError("status document too large")
    return payload


def check_containers(snapshots: dict[str, ContainerSnapshot]) -> CheckResult:
    restart_counts = {
        name: snapshots[name].restart_count
        for name in sorted(EXPECTED_CONTAINERS)
        if name in snapshots
    }
    running = sum(
        name in snapshots and snapshots[name].state == "running"
        for name in EXPECTED_CONTAINERS
    )
    healthy = set(snapshots) == set(EXPECTED_CONTAINERS) and running == len(
        EXPECTED_CONTAINERS
    )
    return CheckResult(
        "PASS" if healthy else "FAIL",
        "containers_running" if healthy else "container_state_invalid",
        {
            "expected": len(EXPECTED_CONTAINERS),
            "running": running,
            "restart_counts": restart_counts,
        },
    )


def check_runtime_contract(
    snapshots: dict[str, ContainerSnapshot],
) -> CheckResult:
    complete = set(snapshots) == set(EXPECTED_CONTAINERS)
    project_matches = complete and all(
        snapshots[name].project == "aurora-stack" for name in EXPECTED_CONTAINERS
    )
    working_dir_matches = complete and all(
        snapshots[name].working_dir == EXPECTED_ROOT for name in EXPECTED_CONTAINERS
    )
    new_api_mounts = {
        (mount.destination, mount.source, mount.read_write)
        for mount in snapshots.get(
            "new-api", ContainerSnapshot("new-api", "", 0, "", "", ())
        ).mounts
    }
    mihomo_mounts = {
        (mount.destination, mount.source, mount.read_write)
        for mount in snapshots.get(
            "mihomo", ContainerSnapshot("mihomo", "", 0, "", "", ())
        ).mounts
    }
    mounts_match = (
        new_api_mounts
        == {("/data", f"{EXPECTED_ROOT}/data/new-api", True)}
        and mihomo_mounts
        == {(
            "/root/.config/mihomo",
            f"{EXPECTED_ROOT}/data/mihomo",
            True,
        )}
    )
    healthy = project_matches and working_dir_matches and mounts_match
    return CheckResult(
        "PASS" if healthy else "FAIL",
        "runtime_matches" if healthy else "runtime_mismatch",
        {
            "project_matches": project_matches,
            "working_dir_matches": working_dir_matches,
            "mounts_match": mounts_match,
        },
    )


STATE_FORMAT = '{{.Name}}\t{{.State.Status}}\t{{.RestartCount}}\t{{index .Config.Labels "com.docker.compose.project"}}\t{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
MOUNT_FORMAT = '{{range .Mounts}}{{printf "%s\\t%s\\t%t\\n" .Destination .Source .RW}}{{end}}'


def run_command(
    args: list[str], timeout: int = 20
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )


def collect_container_snapshots(
    run: CommandRunner = run_command,
) -> dict[str, ContainerSnapshot]:
    snapshots = {}
    for expected_name in EXPECTED_CONTAINERS:
        state = run(
            ["docker", "inspect", "--format", STATE_FORMAT, expected_name], 20
        )
        mounts = run(
            ["docker", "inspect", "--format", MOUNT_FORMAT, expected_name], 20
        )
        if state.returncode != 0 or mounts.returncode != 0:
            raise RuntimeError("container_inspect_failed")
        fields = state.stdout.rstrip("\r\n").split("\t")
        if len(fields) != 5:
            raise RuntimeError("container_inspect_failed")
        name = fields[0].removeprefix("/")
        try:
            restart_count = int(fields[2])
        except ValueError as exc:
            raise RuntimeError("container_inspect_failed") from exc
        if name != expected_name or restart_count < 0:
            raise RuntimeError("container_inspect_failed")
        parsed_mounts = []
        seen_mounts = set()
        for line in mounts.stdout.splitlines():
            if line == "":
                continue
            mount_fields = line.split("\t")
            if len(mount_fields) != 3 or mount_fields[2] not in {"true", "false"}:
                raise RuntimeError("container_inspect_failed")
            signature = tuple(mount_fields)
            if signature in seen_mounts:
                raise RuntimeError("container_inspect_failed")
            seen_mounts.add(signature)
            parsed_mounts.append(
                MountSnapshot(
                    mount_fields[0],
                    mount_fields[1],
                    mount_fields[2] == "true",
                )
            )
        snapshots[name] = ContainerSnapshot(
            name,
            fields[1],
            restart_count,
            fields[3],
            fields[4],
            tuple(parsed_mounts),
        )
    return snapshots


PORT_SPECS = {
    "aurora": ("aurora", 8080),
    "new-api": ("new-api", 3000),
    "metacubexd": ("metacubexd", 80),
    "mihomo": ("mihomo", 9090),
}
CONTAINER_IP_FORMAT = (
    "{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}"
)


def _parse_binding(line: str) -> tuple[str, int]:
    value = line.strip()
    if value.startswith("[") and "]:" in value:
        host, port_text = value[1:].split("]:", 1)
    else:
        host, port_text = value.rsplit(":", 1)
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    ipaddress.ip_address(host)
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError("invalid port")
    return host, int(port_text)


def discover_tcp_targets(
    run: CommandRunner = run_command,
) -> dict[str, tuple[str, int]]:
    targets = {}
    for service, (container, container_port) in PORT_SPECS.items():
        completed = run(
            ["docker", "port", container, f"{container_port}/tcp"], 10
        )
        if completed.returncode != 0:
            raise RuntimeError("tcp_target_unavailable")
        candidates = []
        for line in completed.stdout.splitlines():
            try:
                candidates.append(_parse_binding(line))
            except (IndexError, ValueError):
                continue
        if not candidates:
            raise RuntimeError("tcp_target_unavailable")
        candidates.sort(key=lambda item: (":" in item[0], item[0], item[1]))
        if service != "mihomo":
            targets[service] = candidates[0]
            continue
        inspected = run(
            [
                "docker",
                "inspect",
                "--format",
                CONTAINER_IP_FORMAT,
                container,
            ],
            10,
        )
        if inspected.returncode != 0:
            raise RuntimeError("tcp_target_unavailable")
        bridge_addresses = []
        for line in inspected.stdout.splitlines():
            try:
                address = ipaddress.ip_address(line.strip())
            except ValueError:
                continue
            if (
                address.version == 4
                and not address.is_unspecified
                and not address.is_loopback
                and not address.is_multicast
            ):
                bridge_addresses.append(address)
        if not bridge_addresses:
            raise RuntimeError("tcp_target_unavailable")
        targets[service] = (str(sorted(bridge_addresses)[0]), container_port)
    return targets


def check_local_tcp(
    targets: dict[str, tuple[str, int]],
    connect: TcpConnector = socket.create_connection,
) -> CheckResult:
    services = {}
    for service in sorted(PORT_SPECS):
        connection = None
        try:
            connection = connect(targets[service], 2)
            services[service] = True
        except (KeyError, OSError, TimeoutError):
            services[service] = False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    services[service] = False
    reachable = sum(services.values())
    healthy = reachable == len(PORT_SPECS)
    return CheckResult(
        "PASS" if healthy else "FAIL",
        "local_ports_reachable" if healthy else "local_port_unreachable",
        {
            "expected": len(PORT_SPECS),
            "reachable": reachable,
            "services": services,
        },
    )


def _epoch_utc_text(epoch: int) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def check_database(root: Path, channel_id: int, now_epoch: int) -> CheckResult:
    integrity_ok = False
    try:
        path = root / "data" / "new-api" / "one-api.db"
        database = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30
        )
        try:
            integrity_ok = database.execute("PRAGMA integrity_check").fetchone() == (
                "ok",
            )
            channel = database.execute(
                "SELECT status, key, base_url FROM channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
        finally:
            database.close()
        if channel is not None and len(channel) == 3:
            active = channel[0] == 1
            channel_key = channel[1]
            channel_base = channel[2]
            expected_service_key = None
            try:
                expected_service_key = read_env_value(
                    root / ".env.canary", "AURORA_CANARY_AUTHORIZATION"
                )
            except (OSError, ValueError):
                pass
            service_key_matches = (
                isinstance(channel_key, str)
                and isinstance(expected_service_key, str)
                and hmac.compare_digest(channel_key, expected_service_key)
            )
            service_mode = (
                channel_base == SESSION_CANARY_BASE_URL or service_key_matches
            )
            if service_mode:
                details = {
                    "integrity_ok": integrity_ok,
                    "channel_active": active,
                    "channel_base_matches": channel_base == SESSION_CANARY_BASE_URL,
                    "service_key_matches": service_key_matches,
                }
                healthy = all(details.values())
                return CheckResult(
                    "PASS" if healthy else "FAIL",
                    "database_and_service_key_valid" if healthy else "database_invalid",
                    details,
                )
        if (
            not integrity_ok
            or channel is None
            or channel[0] != 1
            or not isinstance(channel[1], str)
        ):
            raise ValueError("database state invalid")
        expires_epoch = jwt_exp(channel[1])
        remaining = expires_epoch - now_epoch
        details = {
            "integrity_ok": True,
            "remaining_seconds": remaining,
            "expires_at": _epoch_utc_text(expires_epoch),
        }
        if remaining <= 0:
            return CheckResult("FAIL", "token_expired", details)
        if remaining <= 72 * 3600:
            return CheckResult("WARN", "token_near_expiry", details)
        return CheckResult("PASS", "database_and_token_valid", details)
    except (
        OSError,
        OverflowError,
        sqlite3.Error,
        TypeError,
        ValueError,
        RefreshError,
    ):
        return CheckResult(
            "FAIL",
            "database_invalid",
            {
                "integrity_ok": integrity_ok,
                "remaining_seconds": 0,
                "expires_at": "1970-01-01T00:00:00Z",
            },
        )


REFRESH_EVENTS = {"refresh_skipped", "refresh_succeeded", "refresh_failed"}
MAX_REFRESH_LOG_BYTES = 1024 * 1024
EMPTY_REFRESH_DETAILS = {
    "event": "missing",
    "valid_records": 0,
    "invalid_records": 0,
    "event_at": "1970-01-01T00:00:00Z",
}


def refresh_lock_is_held(root: Path) -> bool:
    try:
        import fcntl

        path = root / ".secrets" / "refresh_chatgpt_access_token.lock"
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("refresh_lock_unavailable")
        with path.open("r", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
        return False
    except (ImportError, OSError) as exc:
        raise RuntimeError("refresh_lock_unavailable") from exc


def _parse_refresh_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid refresh time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid refresh time")
    return parsed.astimezone(timezone.utc)


def _refresh_result(
    status_value: str,
    code: str,
    event: str,
    valid_records: int = 0,
    invalid_records: int = 0,
    event_at: str = "1970-01-01T00:00:00Z",
) -> CheckResult:
    return CheckResult(
        status_value,
        code,
        {
            "event": event,
            "valid_records": valid_records,
            "invalid_records": invalid_records,
            "event_at": event_at,
        },
    )


def check_refresh_state(
    root: Path,
    now: datetime,
    lock_probe: LockProbe = refresh_lock_is_held,
) -> CheckResult:
    if now.tzinfo is None:
        return _refresh_result("WARN", "refresh_malformed", "invalid_time", 0, 1)
    now_utc = now.astimezone(timezone.utc)
    try:
        if lock_probe(root):
            return _refresh_result(
                "FAIL", "refresh_in_progress_overrun", "refresh_in_progress"
            )
    except RuntimeError:
        return _refresh_result(
            "FAIL", "refresh_lock_unavailable", "lock_unavailable"
        )

    path = root / ".secrets" / "token-refresh.log"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return CheckResult("WARN", "refresh_missing", dict(EMPTY_REFRESH_DETAILS))
    except OSError:
        return _refresh_result("WARN", "refresh_malformed", "invalid_file", 0, 1)
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return _refresh_result("WARN", "refresh_malformed", "invalid_file", 0, 1)
    if metadata.st_size > MAX_REFRESH_LOG_BYTES:
        return _refresh_result("WARN", "refresh_malformed", "oversized", 0, 1)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_REFRESH_LOG_BYTES + 1)
    except OSError:
        return _refresh_result("WARN", "refresh_malformed", "unreadable", 0, 1)
    if len(raw) > MAX_REFRESH_LOG_BYTES:
        return _refresh_result("WARN", "refresh_malformed", "oversized", 0, 1)

    valid = []
    invalid_records = 0
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("invalid refresh record")
            event = item.get("event")
            event_at = _parse_refresh_time(item.get("time"))
            if event not in REFRESH_EVENTS or event_at > now_utc + timedelta(minutes=5):
                raise ValueError("invalid refresh record")
            valid.append((event_at, event))
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_records += 1
    if not valid:
        if invalid_records:
            return _refresh_result(
                "WARN", "refresh_malformed", "missing", 0, invalid_records
            )
        return CheckResult("WARN", "refresh_missing", dict(EMPTY_REFRESH_DETAILS))

    event_at, event = max(valid)
    details_time = _utc_text(event_at)
    if event == "refresh_failed":
        return _refresh_result(
            "FAIL",
            "refresh_failed",
            event,
            len(valid),
            invalid_records,
            details_time,
        )
    if now_utc - event_at > timedelta(hours=13):
        return _refresh_result(
            "WARN",
            "refresh_stale",
            event,
            len(valid),
            invalid_records,
            details_time,
        )
    if invalid_records:
        return _refresh_result(
            "WARN",
            "refresh_malformed",
            event,
            len(valid),
            invalid_records,
            details_time,
        )
    return _refresh_result(
        "PASS", "refresh_recent", event, len(valid), 0, details_time
    )


def collect_results(
    root: Path,
    channel_id: int,
    now: datetime,
    run: CommandRunner = run_command,
    connect: TcpConnector = socket.create_connection,
    lock_probe: LockProbe = refresh_lock_is_held,
) -> dict[str, CheckResult]:
    results = {}
    try:
        snapshots = collect_container_snapshots(run)
        results["containers"] = check_containers(snapshots)
        results["runtime_contract"] = check_runtime_contract(snapshots)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        results["containers"] = CheckResult(
            "FAIL",
            "container_inspect_failed",
            {"expected": 4, "running": 0, "restart_counts": {}},
        )
        results["runtime_contract"] = CheckResult(
            "FAIL",
            "runtime_inspect_failed",
            {
                "project_matches": False,
                "working_dir_matches": False,
                "mounts_match": False,
            },
        )
    try:
        results["local_tcp"] = check_local_tcp(
            discover_tcp_targets(run), connect
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        results["local_tcp"] = CheckResult(
            "FAIL",
            "tcp_target_unavailable",
            {
                "expected": 4,
                "reachable": 0,
                "services": {name: False for name in sorted(PORT_SPECS)},
            },
        )
    try:
        results["database"] = check_database(root, channel_id, int(now.timestamp()))
    except (OSError, OverflowError, RuntimeError, TypeError, ValueError):
        results["database"] = CheckResult(
            "FAIL",
            "database_invalid",
            {
                "integrity_ok": False,
                "remaining_seconds": 0,
                "expires_at": "1970-01-01T00:00:00Z",
            },
        )
    if set(results["database"].details) == SERVICE_KEY_DETAIL_KEYS:
        results["refresh_log"] = _refresh_result(
            "PASS",
            "refresh_not_applicable",
            "not_applicable",
            event_at=_utc_text(now.astimezone(timezone.utc)),
        )
    else:
        try:
            results["refresh_log"] = check_refresh_state(root, now, lock_probe)
        except (OSError, RuntimeError, TypeError, ValueError):
            results["refresh_log"] = _refresh_result(
                "FAIL", "refresh_lock_unavailable", "lock_unavailable"
            )
    return results


def atomic_write(output: Path, payload: bytes) -> None:
    if output.name != "latest.json":
        raise ValueError("output target invalid")
    parent = output.parent
    try:
        metadata = parent.lstat()
    except OSError as exc:
        raise ValueError("output parent invalid") from exc
    if parent.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("output parent invalid")
    try:
        target_metadata = output.lstat()
    except FileNotFoundError:
        target_metadata = None
    if target_metadata is not None and (
        output.is_symlink() or not stat.S_ISREG(target_metadata.st_mode)
    ):
        raise ValueError("output target invalid")

    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".latest.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--channel-id", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        now = datetime.now(timezone.utc)
        results = collect_results(args.root.resolve(), args.channel_id, now)
        document = build_document(results, now)
        atomic_write(args.output, serialize_document(document))
        status_value = document["overall"]
        print(f"aurora_n8n_status={status_value} code=published")
        return 1 if status_value == "FAIL" else 0
    except (OSError, RuntimeError, TypeError, ValueError):
        print(
            "aurora_n8n_status=ERROR code=producer_error",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
