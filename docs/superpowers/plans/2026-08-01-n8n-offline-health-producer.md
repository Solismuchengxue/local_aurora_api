# Solis_Aurora_Gateway n8n Offline Health Producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Do not use subagents unless the user explicitly authorizes them. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Aurora 仓库中实现一个不调用真实 API、只发布严格脱敏 `latest.json` 的 FNOS 本地状态生产器，并以标准库测试证明其检查和原子写入边界。

**Architecture:** 新脚本独立于现有完整健康检查，通过限定的 Docker inspect、本地 TCP、SQLite 只读查询和续期锁/日志元数据形成五项固定检查，再将 Schema v1 文档原子替换到显式指定路径。该子计划只交付 Aurora 生产器；Studio OS 的只读子挂载与 n8n 工作流在其当前维护完成后另建计划。

**Tech Stack:** Python 3 标准库、`unittest`、SQLite 只读 URI、Docker CLI 限定格式、POSIX `flock`、Markdown。

## Global Constraints

- 只修改 `F:\70_Infrastructure_and_Operations\Solis_Aurora_Gateway`；不得修改 `Solis_Studio_OS`。
- 本计划执行阶段只做 Windows 本地实现与模拟验证；不得连接 FNOS、操作容器、修改 cron、导入或触发 n8n、发送邮件或调用任何真实 API。
- 不新增软件包、系统工具、Skill、Plugin、MCP Server 或项目依赖。
- 不导入或调用 `check_stack_health.py` 中的模型、聊天或代理出口函数；只允许复用 `refresh_chatgpt_access_token.jwt_exp`。
- 生产器不得包含 HTTP 客户端、外部域名、模型列表或聊天入口；网络能力仅限注入式本地 TCP 建连。
- 状态不得包含 Token、连接串、邮箱、Cookie、Credential、敏感路径、业务正文、原始日志、原始异常或原始命令输出。
- 活动目录只允许一个滚动 `latest.json`，序列化结果不得超过 16 KiB。
- 每项生产代码先写失败测试并确认因目标能力缺失而失败，再写最小实现。
- 保留现有 `check_stack_health.py` 和 `refresh_chatgpt_access_token.py` 行为，不顺手重构。
- 不暂存、不提交、不 push；因此本计划有意不包含技能模板中的 commit 步骤。
- 未部署前只能称为“本地候选实现”，不得声称 FNOS 或 n8n 已接入。

---

## File Map

- Create: `scripts/write_n8n_health_status.py` — 类型、五项离线检查、Schema v1、原子发布和 CLI。
- Create: `tests/test_write_n8n_health_status.py` — 全部标准库单元测试和安全回归测试。
- Modify: `DESIGN.md` — 记录状态生产者权威、文件契约和未部署事实。
- Modify: `docs/fnos_deployment.md` — 记录候选命令、05:12/17:12 计划、退出码和后续部署门禁。
- Preserve: `scripts/check_stack_health.py`、`scripts/refresh_chatgpt_access_token.py`、`docker-compose.yml`。

## Stable Interfaces

- `CheckResult(status: str, code: str, details: dict[str, object])` — 固定检查结果。
- `MountSnapshot(destination: str, source: str, read_write: bool)` — 单个 Docker 挂载快照。
- `ContainerSnapshot(name: str, state: str, restart_count: int, project: str, working_dir: str, mounts: Sequence[MountSnapshot])` — 单个固定容器快照。
- `overall_status(results: dict[str, CheckResult]) -> str` — 以 `FAIL > WARN > PASS` 聚合。
- `build_document(results: dict[str, CheckResult], generated_at: datetime) -> dict[str, object]` — 构造严格 Schema v1。
- `serialize_document(document: dict[str, object]) -> bytes` — 确定性 UTF-8 编码与 16 KiB 上限。
- `collect_container_snapshots(run: CommandRunner = run_command) -> dict[str, ContainerSnapshot]` — 限定 Docker 适配器。
- `check_containers(snapshots: dict[str, ContainerSnapshot]) -> CheckResult` — 容器状态检查。
- `check_runtime_contract(snapshots: dict[str, ContainerSnapshot]) -> CheckResult` — 项目、工作目录和挂载检查。
- `discover_tcp_targets(run: CommandRunner = run_command) -> dict[str, tuple[str, int]]` — 验证固定发布绑定，并为 Mihomo 发现容器桥接 IPv4。
- `check_local_tcp(targets: dict[str, tuple[str, int]], connect: TcpConnector = socket.create_connection) -> CheckResult` — 仅 TCP 建连。
- `check_database(root: Path, channel_id: int, now_epoch: int) -> CheckResult` — SQLite 与渠道 Token 元数据。
- `refresh_lock_is_held(root: Path) -> bool` — 非阻塞只读锁探测。
- `check_refresh_state(root: Path, now: datetime, lock_probe: LockProbe = refresh_lock_is_held) -> CheckResult` — 锁和日志元数据检查。
- `collect_results(root: Path, channel_id: int, now: datetime, run: CommandRunner = run_command, connect: TcpConnector = socket.create_connection, lock_probe: LockProbe = refresh_lock_is_held) -> dict[str, CheckResult]` — 五项聚合。
- `atomic_write(output: Path, payload: bytes) -> None` — 单文件原子替换。
- `main(argv: list[str] | None = None) -> int` — CLI 与退出码。

---

### Task 1: 固定结果模型、总体状态与 Schema v1

**Files:**
- Create: `tests/test_write_n8n_health_status.py`
- Create: `scripts/write_n8n_health_status.py`

**Interfaces:**
- Produces: `CheckResult`, `overall_status()`, `build_document()`, `serialize_document()`。
- Consumes: 无项目内运行依赖。

- [ ] **Step 1: 写入模块加载器和第一组失败测试**

在测试文件中沿用现有测试的 `importlib` 模式，并写入以下测试。`make_results()` 必须返回五个固定检查名，避免后续测试各自构造不一致的 Schema。

```python
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "write_n8n_health_status.py"
SPEC = importlib.util.spec_from_file_location("n8n_health_status", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def make_results(status: str = "PASS") -> dict[str, object]:
    return {
        "containers": MODULE.CheckResult(status, "containers_running", {"expected": 4, "running": 4}),
        "runtime_contract": MODULE.CheckResult("PASS", "runtime_matches", {"project_matches": True, "working_dir_matches": True, "mounts_match": True}),
        "local_tcp": MODULE.CheckResult("PASS", "local_ports_reachable", {"expected": 4, "reachable": 4}),
        "database": MODULE.CheckResult("PASS", "database_and_token_valid", {"integrity_ok": True, "remaining_seconds": 604800, "expires_at": "2026-08-08T09:12:00Z"}),
        "refresh_log": MODULE.CheckResult("PASS", "refresh_recent", {"event": "refresh_skipped", "valid_records": 1, "invalid_records": 0, "event_at": "2026-08-01T09:07:00Z"}),
    }


class DocumentTests(unittest.TestCase):
    def test_overall_status_uses_fail_then_warn_then_pass(self):
        self.assertEqual(MODULE.overall_status(make_results()), "PASS")
        warning = make_results()
        warning["database"] = MODULE.CheckResult("WARN", "token_near_expiry", {"integrity_ok": True, "remaining_seconds": 3600, "expires_at": "2026-08-01T10:12:00Z"})
        self.assertEqual(MODULE.overall_status(warning), "WARN")
        warning["containers"] = MODULE.CheckResult("FAIL", "container_state_invalid", {"expected": 4, "running": 3})
        self.assertEqual(MODULE.overall_status(warning), "FAIL")

    def test_document_has_exact_schema_and_deterministic_bytes(self):
        now = datetime(2026, 8, 1, 9, 12, tzinfo=timezone.utc)
        document = MODULE.build_document(make_results(), now)
        self.assertEqual(set(document), {"schema_version", "producer", "generated_at", "overall", "checks"})
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["producer"], "Solis_Aurora_Gateway")
        self.assertEqual(document["generated_at"], "2026-08-01T09:12:00Z")
        payload = MODULE.serialize_document(document)
        self.assertLessEqual(len(payload), 16 * 1024)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(json.loads(payload), document)
        self.assertEqual(payload, MODULE.serialize_document(document))

    def test_unknown_status_and_wrong_check_set_fail_closed(self):
        with self.assertRaises(ValueError):
            MODULE.CheckResult("UNKNOWN", "containers_running", {})
        results = make_results()
        del results["local_tcp"]
        with self.assertRaises(ValueError):
            MODULE.build_document(results, datetime.now(timezone.utc))
        results = make_results()
        results["containers"] = MODULE.CheckResult("PASS", "unapproved_code", {"expected": 4, "running": 4})
        with self.assertRaises(ValueError):
            MODULE.build_document(results, datetime.now(timezone.utc))
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
python -m unittest tests.test_write_n8n_health_status.DocumentTests -v
```

Expected: `FileNotFoundError` 指向尚未创建的 `scripts/write_n8n_health_status.py`。这证明测试因生产器缺失而失败，而不是因为测试断言错误。

- [ ] **Step 3: 写入最小结果模型和序列化实现**

实现以下常量和行为：

```python
import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
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

from refresh_chatgpt_access_token import RefreshError, jwt_exp


STATUS_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}
EXPECTED_CHECKS = ("containers", "runtime_contract", "local_tcp", "database", "refresh_log")
ALLOWED_CODES = {
    "containers": {"containers_running", "container_state_invalid", "container_inspect_failed"},
    "runtime_contract": {"runtime_matches", "runtime_mismatch", "runtime_inspect_failed"},
    "local_tcp": {"local_ports_reachable", "local_port_unreachable", "tcp_target_unavailable"},
    "database": {"database_and_token_valid", "token_near_expiry", "token_expired", "database_invalid"},
    "refresh_log": {"refresh_recent", "refresh_missing", "refresh_stale", "refresh_malformed", "refresh_failed", "refresh_in_progress_overrun", "refresh_lock_unavailable"},
}
SCHEMA_VERSION = 1
PRODUCER = "Solis_Aurora_Gateway"
MAX_STATUS_BYTES = 16 * 1024

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
```

`build_document()` 必须拒绝不精确的检查集合和检查名不匹配的状态码，将 UTC 时间格式化为秒级 `Z`，并只用 `dataclasses.asdict()` 转换固定结果。`serialize_document()` 使用 `ensure_ascii=False`、`sort_keys=True`、紧凑分隔符和结尾换行；超过 16 KiB 时抛出 `ValueError("status document too large")`。

- [ ] **Step 4: 运行测试并确认 GREEN**

Run:

```powershell
python -m unittest tests.test_write_n8n_health_status.DocumentTests -v
```

Expected: 3 tests pass，0 failures，0 errors。

---

### Task 2: Docker 容器与运行契约

**Files:**
- Modify: `tests/test_write_n8n_health_status.py`
- Modify: `scripts/write_n8n_health_status.py`

**Interfaces:**
- Produces: `MountSnapshot`, `ContainerSnapshot`, `collect_container_snapshots()`, `check_containers()`, `check_runtime_contract()`。
- Consumes: Task 1 的 `CheckResult`。

- [ ] **Step 1: 为纯检查函数写失败测试**

```python
class DockerCheckTests(unittest.TestCase):
    def snapshots(self):
        root = "/vol1/1000/Solis_Aurora_Gateway"
        return {
            "aurora": MODULE.ContainerSnapshot("aurora", "running", 0, "aurora-stack", root, ()),
            "new-api": MODULE.ContainerSnapshot("new-api", "running", 1, "aurora-stack", root, (MODULE.MountSnapshot("/data", f"{root}/data/new-api", True),)),
            "mihomo": MODULE.ContainerSnapshot("mihomo", "running", 0, "aurora-stack", root, (MODULE.MountSnapshot("/root/.config/mihomo", f"{root}/data/mihomo", True),)),
            "metacubexd": MODULE.ContainerSnapshot("metacubexd", "running", 0, "aurora-stack", root, ()),
        }

    def test_running_containers_pass_and_restart_count_is_metadata_only(self):
        result = MODULE.check_containers(self.snapshots())
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.code, "containers_running")
        self.assertEqual(result.details["running"], 4)
        self.assertEqual(result.details["restart_counts"]["new-api"], 1)

    def test_missing_or_stopped_container_fails(self):
        snapshots = self.snapshots()
        snapshots["aurora"] = MODULE.ContainerSnapshot("aurora", "exited", 0, "aurora-stack", "/vol1/1000/Solis_Aurora_Gateway", ())
        result = MODULE.check_containers(snapshots)
        self.assertEqual((result.status, result.code), ("FAIL", "container_state_invalid"))

    def test_runtime_contract_requires_exact_project_workdir_and_mounts(self):
        self.assertEqual(MODULE.check_runtime_contract(self.snapshots()).status, "PASS")
        snapshots = self.snapshots()
        snapshots["new-api"] = MODULE.ContainerSnapshot("new-api", "running", 0, "wrong", "/vol1/1000/Solis_Aurora_Gateway", snapshots["new-api"].mounts)
        result = MODULE.check_runtime_contract(snapshots)
        self.assertEqual((result.status, result.code), ("FAIL", "runtime_mismatch"))
        self.assertEqual(set(result.details), {"project_matches", "working_dir_matches", "mounts_match"})
```

- [ ] **Step 2: 运行 DockerCheckTests 并确认 RED**

Run: `python -m unittest tests.test_write_n8n_health_status.DockerCheckTests -v`

Expected: `AttributeError`，因为快照类型和检查函数尚不存在。

- [ ] **Step 3: 实现纯检查函数**

固定容器集合为 `aurora/new-api/mihomo/metacubexd`。`check_containers()` 只因缺失或非 `running` 失败；重启次数只进入按固定容器名排序的整数详情。`check_runtime_contract()` 只输出三个布尔值，不输出实际项目、路径或挂载源。

```python
EXPECTED_CONTAINERS = ("aurora", "new-api", "mihomo", "metacubexd")
EXPECTED_ROOT = "/vol1/1000/Solis_Aurora_Gateway"


def check_containers(snapshots: dict[str, ContainerSnapshot]) -> CheckResult:
    restart_counts = {
        name: snapshots[name].restart_count
        for name in EXPECTED_CONTAINERS
        if name in snapshots
    }
    running = sum(
        name in snapshots and snapshots[name].state == "running"
        for name in EXPECTED_CONTAINERS
    )
    healthy = set(snapshots) == set(EXPECTED_CONTAINERS) and running == 4
    return CheckResult(
        "PASS" if healthy else "FAIL",
        "containers_running" if healthy else "container_state_invalid",
        {"expected": 4, "running": running, "restart_counts": restart_counts},
    )


def check_runtime_contract(snapshots: dict[str, ContainerSnapshot]) -> CheckResult:
    complete = set(snapshots) == set(EXPECTED_CONTAINERS)
    project_matches = complete and all(
        snapshots[name].project == "aurora-stack" for name in EXPECTED_CONTAINERS
    )
    working_dir_matches = complete and all(
        snapshots[name].working_dir == EXPECTED_ROOT for name in EXPECTED_CONTAINERS
    )
    new_api_mounts = {
        (mount.destination, mount.source, mount.read_write)
        for mount in snapshots.get("new-api", ContainerSnapshot("new-api", "", 0, "", "", ())).mounts
    }
    mihomo_mounts = {
        (mount.destination, mount.source, mount.read_write)
        for mount in snapshots.get("mihomo", ContainerSnapshot("mihomo", "", 0, "", "", ())).mounts
    }
    mounts_match = (
        ("/data", f"{EXPECTED_ROOT}/data/new-api", True) in new_api_mounts
        and ("/root/.config/mihomo", f"{EXPECTED_ROOT}/data/mihomo", True) in mihomo_mounts
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
```

- [ ] **Step 4: 为 Docker 适配器写失败测试**

增加一个记录参数并返回固定 `CompletedProcess` 的 fake runner。断言适配器只执行 `docker inspect`，只读取 `.Name`、`.State.Status`、`.RestartCount`、两个明确 Compose 标签及 `.Mounts`；参数中不得出现 `Config.Env`、完整 `json .` 或 `com.docker.compose.project.config_files`。

```python
def test_collect_snapshots_uses_only_bounded_inspect_formats(self):
    calls = []
    outputs = iter([
        "/aurora\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
        "",
        "/new-api\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
        "/data\t/vol1/1000/Solis_Aurora_Gateway/data/new-api\ttrue\n",
        "/mihomo\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
        "/root/.config/mihomo\t/vol1/1000/Solis_Aurora_Gateway/data/mihomo\ttrue\n",
        "/metacubexd\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
        "",
    ])

    def fake_run(args, timeout=20):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, next(outputs), "")

    snapshots = MODULE.collect_container_snapshots(fake_run)
    self.assertEqual(set(snapshots), {"aurora", "new-api", "mihomo", "metacubexd"})
    rendered = " ".join(" ".join(call) for call in calls)
    self.assertNotIn("Config.Env", rendered)
    self.assertNotIn("json .", rendered)
    self.assertNotIn("project.config_files", rendered)
```

- [ ] **Step 5: 实现限定 Docker 适配器并确认 GREEN**

`run_command()` 必须捕获 stdout、丢弃 stderr、设置超时且不抛出 `check=True` 异常。每个固定容器执行一次状态/标签 inspect 和一次挂载 inspect；任何非零返回、字段数错误、未知名称、非整数重启数或重复挂载都抛出内部固定异常，由 `collect_results()` 在 Task 5 转换为固定失败码。

```python
STATE_FORMAT = '{{.Name}}\t{{.State.Status}}\t{{.RestartCount}}\t{{index .Config.Labels "com.docker.compose.project"}}\t{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
MOUNT_FORMAT = '{{range .Mounts}}{{printf "%s\\t%s\\t%t\\n" .Destination .Source .RW}}{{end}}'


def run_command(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        check=False,
    )


def collect_container_snapshots(run: CommandRunner = run_command) -> dict[str, ContainerSnapshot]:
    snapshots = {}
    for expected_name in EXPECTED_CONTAINERS:
        state = run(["docker", "inspect", "--format", STATE_FORMAT, expected_name], 20)
        mounts = run(["docker", "inspect", "--format", MOUNT_FORMAT, expected_name], 20)
        if state.returncode != 0 or mounts.returncode != 0:
            raise RuntimeError("container_inspect_failed")
        fields = state.stdout.rstrip("\r\n").split("\t")
        if len(fields) != 5 or fields[0] != f"/{expected_name}" or not fields[2].isdigit():
            raise RuntimeError("container_inspect_failed")
        parsed_mounts = []
        seen_destinations = set()
        for line in mounts.stdout.splitlines():
            mount_fields = line.split("\t")
            if len(mount_fields) != 3 or mount_fields[0] in seen_destinations:
                raise RuntimeError("container_inspect_failed")
            if mount_fields[2] not in {"true", "false"}:
                raise RuntimeError("container_inspect_failed")
            seen_destinations.add(mount_fields[0])
            parsed_mounts.append(MountSnapshot(mount_fields[0], mount_fields[1], mount_fields[2] == "true"))
        snapshots[expected_name] = ContainerSnapshot(
            expected_name,
            fields[1],
            int(fields[2]),
            fields[3],
            fields[4],
            tuple(parsed_mounts),
        )
    return snapshots
```

Run: `python -m unittest tests.test_write_n8n_health_status.DockerCheckTests -v`

Expected: 4 tests pass。

---

### Task 3: 本地 TCP 检查

**Files:**
- Modify: `tests/test_write_n8n_health_status.py`
- Modify: `scripts/write_n8n_health_status.py`

**Interfaces:**
- Produces: `discover_tcp_targets()`, `check_local_tcp()`。
- Consumes: Task 1 的 `CheckResult` 和 Task 2 的 `run_command()`。

- [ ] **Step 1: 写入地址规范化与连接结果的失败测试**

```python
class TcpCheckTests(unittest.TestCase):
    def test_all_fixed_targets_reachable(self):
        targets = {
            "aurora": ("127.0.0.1", 8080),
            "new-api": ("127.0.0.1", 3000),
            "metacubexd": ("127.0.0.1", 9097),
            "mihomo": ("192.0.2.10", 9090),
        }
        calls = []

        class Connection:
            def close(self):
                return None

        def connect(target, timeout):
            calls.append((target, timeout))
            return Connection()

        result = MODULE.check_local_tcp(targets, connect)
        self.assertEqual((result.status, result.code), ("PASS", "local_ports_reachable"))
        self.assertEqual(result.details, {"expected": 4, "reachable": 4, "services": {"aurora": True, "metacubexd": True, "mihomo": True, "new-api": True}})
        self.assertTrue(all(timeout == 2 for _, timeout in calls))

    def test_unreachable_target_fails_without_exposing_address(self):
        targets = {name: ("127.0.0.1", port) for name, port in {"aurora": 8080, "new-api": 3000, "metacubexd": 9097, "mihomo": 9090}.items()}

        def connect(target, timeout):
            if target[1] == 3000:
                raise OSError("sensitive raw socket error")
            return mock.MagicMock()

        result = MODULE.check_local_tcp(targets, connect)
        self.assertEqual((result.status, result.code), ("FAIL", "local_port_unreachable"))
        self.assertNotIn("127.0.0.1", json.dumps(result.details))
        self.assertNotIn("sensitive", json.dumps(result.details))
```

- [ ] **Step 2: 运行 TcpCheckTests 并确认 RED**

Run: `python -m unittest tests.test_write_n8n_health_status.TcpCheckTests -v`

Expected: `AttributeError` 指向缺失的 `check_local_tcp`。

- [ ] **Step 3: 实现 TCP 纯检查和 Docker 端口发现**

`check_local_tcp()` 必须按固定服务名排序，以 2 秒超时调用 connector，并在 `finally` 关闭返回连接；只输出服务布尔值和计数。`discover_tcp_targets()` 使用限定 Docker 端口格式验证四个固定容器端口，将 `0.0.0.0` 规范为 `127.0.0.1`、`::` 规范为 `::1`，使用 `ipaddress.ip_address()` 和 `1..65535` 验证。FNOS 现场证明 Mihomo 的指定 LAN 发布地址不支持主机自回环，因此 Mihomo 在发布绑定门禁通过后，另用限定 `docker inspect` 读取容器桥接 IPv4 作为 TCP 目标；不得把任一实际地址写入状态详情。

```python
PORT_SPECS = {
    "aurora": ("aurora", 8080),
    "new-api": ("new-api", 3000),
    "metacubexd": ("metacubexd", 80),
    "mihomo": ("mihomo", 9090),
}
CONTAINER_IP_FORMAT = "{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}"


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


def discover_tcp_targets(run: CommandRunner = run_command) -> dict[str, tuple[str, int]]:
    targets = {}
    for service, (container, container_port) in PORT_SPECS.items():
        completed = run(["docker", "port", container, f"{container_port}/tcp"], 10)
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
        inspected = run(["docker", "inspect", "--format", CONTAINER_IP_FORMAT, container], 10)
        if inspected.returncode != 0:
            raise RuntimeError("tcp_target_unavailable")
        bridge_addresses = []
        for line in inspected.stdout.splitlines():
            try:
                address = ipaddress.ip_address(line.strip())
            except ValueError:
                continue
            if address.version == 4 and not address.is_unspecified and not address.is_loopback and not address.is_multicast:
                bridge_addresses.append(address)
        if not bridge_addresses:
            raise RuntimeError("tcp_target_unavailable")
        targets[service] = (str(sorted(bridge_addresses)[0]), container_port)
    return targets


def check_local_tcp(targets: dict[str, tuple[str, int]], connect: TcpConnector = socket.create_connection) -> CheckResult:
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
                connection.close()
    reachable = sum(services.values())
    healthy = reachable == len(PORT_SPECS)
    return CheckResult(
        "PASS" if healthy else "FAIL",
        "local_ports_reachable" if healthy else "local_port_unreachable",
        {"expected": len(PORT_SPECS), "reachable": reachable, "services": services},
    )
```

- [ ] **Step 4: 增加端口发现的格式失败测试并确认 GREEN**

覆盖空绑定、非 IP、非数字端口、端口越界和 Docker 非零退出，期望适配器抛出固定 `RuntimeError("tcp_target_unavailable")`，且错误中不包含原始字段。

Run: `python -m unittest tests.test_write_n8n_health_status.TcpCheckTests -v`

Expected: 全部 TCP 测试通过。

---

### Task 4: SQLite、Token 到期和续期锁/日志

**Files:**
- Modify: `tests/test_write_n8n_health_status.py`
- Modify: `scripts/write_n8n_health_status.py`

**Interfaces:**
- Produces: `check_database()`, `refresh_lock_is_held()`, `check_refresh_state()`。
- Consumes: `refresh_chatgpt_access_token.jwt_exp`，不得调用该模块的网络、写入或重启函数。

- [ ] **Step 1: 写 SQLite RED 测试**

测试辅助函数必须创建最小 `channels(id, status, key)` 表和合成 JWT；不得创建客户端 Token 表，因为生产器不需要客户端令牌。

```python
def make_token(exp: int) -> str:
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


class DatabaseCheckTests(unittest.TestCase):
    def test_database_pass_warn_and_fail_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data" / "new-api" / "one-api.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE channels (id INTEGER PRIMARY KEY, status INTEGER, key TEXT)")
                connection.execute("INSERT INTO channels VALUES (1, 1, ?)", (make_token(500000),))
            self.assertEqual(MODULE.check_database(root, 1, 100000).status, "PASS")
            self.assertEqual(MODULE.check_database(root, 1, 496500).code, "token_near_expiry")
            self.assertEqual(MODULE.check_database(root, 1, 500000).status, "FAIL")
```

- [ ] **Step 2: 运行 DatabaseCheckTests 并确认 RED**

Run: `python -m unittest tests.test_write_n8n_health_status.DatabaseCheckTests -v`

Expected: `AttributeError` 指向缺失的 `check_database`。

- [ ] **Step 3: 实现 SQLite 只读检查并确认 GREEN**

使用 `file:<posix-path>?mode=ro`、30 秒 SQLite timeout、`PRAGMA integrity_check` 和参数化渠道查询。仅在内存中把渠道 key 传给 `jwt_exp()`；任何 SQLite、类型或 JWT 错误统一返回 `FAIL / database_invalid`。详情只允许 `integrity_ok`、`remaining_seconds` 和 UTC `expires_at`。

```python
def _utc_text(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def check_database(root: Path, channel_id: int, now_epoch: int) -> CheckResult:
    integrity_ok = False
    try:
        path = root / "data" / "new-api" / "one-api.db"
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30) as database:
            integrity_ok = database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            channel = database.execute(
                "SELECT key FROM channels WHERE id = ? AND status = 1",
                (channel_id,),
            ).fetchone()
        if not integrity_ok or channel is None or not isinstance(channel[0], str):
            raise ValueError("database state invalid")
        expires_epoch = jwt_exp(channel[0])
        remaining = expires_epoch - now_epoch
        details = {
            "integrity_ok": True,
            "remaining_seconds": remaining,
            "expires_at": _utc_text(expires_epoch),
        }
        if remaining <= 0:
            return CheckResult("FAIL", "token_expired", details)
        if remaining <= 72 * 3600:
            return CheckResult("WARN", "token_near_expiry", details)
        return CheckResult("PASS", "database_and_token_valid", details)
    except (OSError, sqlite3.Error, TypeError, ValueError, RefreshError):
        return CheckResult(
            "FAIL",
            "database_invalid",
            {"integrity_ok": integrity_ok, "remaining_seconds": 0, "expires_at": "1970-01-01T00:00:00Z"},
        )
```

Run: `python -m unittest tests.test_write_n8n_health_status.DatabaseCheckTests -v`

Expected: 边界测试通过。

- [ ] **Step 4: 写续期锁和日志 RED 测试**

```python
class RefreshStateTests(unittest.TestCase):
    def test_held_lock_fails_before_log_read(self):
        with tempfile.TemporaryDirectory() as directory:
            result = MODULE.check_refresh_state(Path(directory), datetime(2026, 8, 1, 9, 12, tzinfo=timezone.utc), lambda path: True)
        self.assertEqual((result.status, result.code), ("FAIL", "refresh_in_progress_overrun"))

    def test_recent_success_passes_and_raw_reason_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / ".secrets" / "token-refresh.log"
            log.parent.mkdir(parents=True)
            log.write_text('{"time":"2026-08-01T09:17:00Z","event":"refresh_skipped","reason":"raw-private-text"}\n', encoding="utf-8")
            result = MODULE.check_refresh_state(root, datetime(2026, 8, 1, 9, 20, tzinfo=timezone.utc), lambda path: False)
        self.assertEqual((result.status, result.code), ("PASS", "refresh_recent"))
        self.assertNotIn("raw-private-text", json.dumps(result.details))

    def test_partial_invalid_log_warns_and_failed_event_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / ".secrets" / "token-refresh.log"
            log.parent.mkdir(parents=True)
            log.write_text('not-json\n{"time":"2026-08-01T09:17:00Z","event":"refresh_failed"}\n', encoding="utf-8")
            result = MODULE.check_refresh_state(root, datetime(2026, 8, 1, 9, 20, tzinfo=timezone.utc), lambda path: False)
        self.assertEqual((result.status, result.code), ("FAIL", "refresh_failed"))
        self.assertEqual(result.details["invalid_records"], 1)
```

- [ ] **Step 5: 运行 RefreshStateTests 并确认 RED**

Run: `python -m unittest tests.test_write_n8n_health_status.RefreshStateTests -v`

Expected: `AttributeError` 指向缺失的 `check_refresh_state`。

- [ ] **Step 6: 实现锁探测和白名单日志解析**

`refresh_lock_is_held()` 在函数内部导入 `fcntl`，以只读模式打开既有锁文件并尝试 `LOCK_SH | LOCK_NB`；锁被占用返回 `True`，成功取得后立即释放并返回 `False`。文件缺失时不得创建锁文件：先用 `lstat()` 检查，不存在则返回 `False`。其他 OS/权限错误必须转换为固定 `RuntimeError("refresh_lock_unavailable")`，并由检查函数转换为 `FAIL / refresh_lock_unavailable`。

`check_refresh_state()` 先读取文件大小元数据，再以二进制方式最多读取 `1 MiB + 1 byte`；超过上限固定返回 `WARN / refresh_malformed`，不得把超长正文载入状态。逐行只接受 JSON object、白名单事件 `refresh_skipped/refresh_succeeded/refresh_failed` 和可解析 UTC 时间。忽略 `reason` 及所有未知字段。规则严格按设计实现：锁占用为 `FAIL`；失败事件为 `FAIL`；13 小时以上为 `WARN / refresh_stale`；缺失为空为 `WARN / refresh_missing`；近期合法事件夹杂非法行为 `WARN / refresh_malformed`；否则 `PASS / refresh_recent`。

```python
REFRESH_EVENTS = {"refresh_skipped", "refresh_succeeded", "refresh_failed"}
MAX_REFRESH_LOG_BYTES = 1024 * 1024


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


def check_refresh_state(root: Path, now: datetime, lock_probe: LockProbe = refresh_lock_is_held) -> CheckResult:
    try:
        if lock_probe(root):
            return CheckResult("FAIL", "refresh_in_progress_overrun", {"event": "refresh_in_progress", "valid_records": 0, "invalid_records": 0, "event_at": "1970-01-01T00:00:00Z"})
    except RuntimeError:
        return CheckResult("FAIL", "refresh_lock_unavailable", {"event": "lock_unavailable", "valid_records": 0, "invalid_records": 0, "event_at": "1970-01-01T00:00:00Z"})
    path = root / ".secrets" / "token-refresh.log"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return CheckResult("WARN", "refresh_missing", {"event": "missing", "valid_records": 0, "invalid_records": 0, "event_at": "1970-01-01T00:00:00Z"})
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return CheckResult("WARN", "refresh_malformed", {"event": "invalid_file", "valid_records": 0, "invalid_records": 1, "event_at": "1970-01-01T00:00:00Z"})
    if metadata.st_size > MAX_REFRESH_LOG_BYTES:
        return CheckResult("WARN", "refresh_malformed", {"event": "oversized", "valid_records": 0, "invalid_records": 1, "event_at": "1970-01-01T00:00:00Z"})
    valid = []
    invalid_records = 0
    with path.open("rb") as stream:
        raw = stream.read(MAX_REFRESH_LOG_BYTES + 1)
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(line)
            event = item.get("event") if isinstance(item, dict) else None
            event_at = _parse_refresh_time(item.get("time") if isinstance(item, dict) else None)
            if event not in REFRESH_EVENTS:
                raise ValueError("invalid refresh event")
            valid.append((event_at, event))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            invalid_records += 1
    if not valid:
        return CheckResult("WARN", "refresh_missing", {"event": "missing", "valid_records": 0, "invalid_records": invalid_records, "event_at": "1970-01-01T00:00:00Z"})
    event_at, event = max(valid)
    details = {"event": event, "valid_records": len(valid), "invalid_records": invalid_records, "event_at": event_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    if event == "refresh_failed":
        return CheckResult("FAIL", "refresh_failed", details)
    if now.astimezone(timezone.utc) - event_at > timedelta(hours=13):
        return CheckResult("WARN", "refresh_stale", details)
    if invalid_records:
        return CheckResult("WARN", "refresh_malformed", details)
    return CheckResult("PASS", "refresh_recent", details)
```

- [ ] **Step 7: 补齐缺失、空、过期、非法时间与 1 MiB 上限测试并确认 GREEN**

Run: `python -m unittest tests.test_write_n8n_health_status.RefreshStateTests -v`

Expected: 全部续期状态测试通过，且任何原始行或异常正文都不在结果中。

---

### Task 5: 聚合、原子发布与 CLI

**Files:**
- Modify: `tests/test_write_n8n_health_status.py`
- Modify: `scripts/write_n8n_health_status.py`

**Interfaces:**
- Produces: `collect_results()`, `atomic_write()`, `parse_args()`, `main()`。
- Consumes: Tasks 1–4 全部稳定接口。

- [ ] **Step 1: 写聚合和依赖失败 RED 测试**

通过 mock 注入五项边界，断言即使 Docker 适配器失败，数据库和续期检查仍被调用；适配器异常只转换为对应固定 `FAIL` 码，不把异常文本写入结果。五项键必须始终精确存在。

- [ ] **Step 2: 写原子发布 RED 测试**

```python
class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_replaces_old_file_and_leaves_no_history(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            output.write_bytes(b"old\n")
            MODULE.atomic_write(output, b'{"schema_version":1}\n')
            self.assertEqual(output.read_bytes(), b'{"schema_version":1}\n')
            self.assertEqual([path.name for path in output.parent.iterdir()], ["latest.json"])

    def test_missing_or_symlink_parent_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                MODULE.atomic_write(root / "missing" / "latest.json", b"{}\n")
            target = root / "real"
            target.mkdir()
            link = root / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks unavailable")
            with self.assertRaises(ValueError):
                MODULE.atomic_write(link / "latest.json", b"{}\n")
```

- [ ] **Step 3: 运行聚合与原子测试并确认 RED**

Run:

```powershell
python -m unittest tests.test_write_n8n_health_status.AtomicWriteTests -v
```

Expected: `AttributeError` 指向缺失的 `atomic_write`。

- [ ] **Step 4: 实现原子写入**

严格执行：父目录 `lstat()` 必须为非符号链接普通目录；既有目标若存在必须为非符号链接普通文件；使用 `tempfile.mkstemp()` 在同目录创建唯一临时文件；POSIX 上 `fchmod(0600)`；完整写入、`flush`、文件 `fsync` 后 `os.replace()`；支持时刷新父目录。异常时只删除本次临时文件，保留旧目标。

```python
def atomic_write(output: Path, payload: bytes) -> None:
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
    if target_metadata is not None:
        if output.is_symlink() or not stat.S_ISREG(target_metadata.st_mode):
            raise ValueError("output target invalid")
    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".latest.", suffix=".tmp", dir=parent)
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
```

- [ ] **Step 5: 写 CLI RED 测试**

mock `collect_results()` 和 `atomic_write()`，分别覆盖 `PASS/WARN/FAIL` 及发布异常：

- PASS/WARN 成功发布返回 `0`；
- FAIL 成功发布返回 `1`；
- 收集或发布异常返回 `2`；
- stdout/stderr 只允许固定 `aurora_n8n_status=<STATUS> code=<fixed-code>`，不得包含异常正文；
- `--output` 缺失由 argparse 返回 `2`；
- `--root` 默认脚本父目录的父目录，`--channel-id` 默认 `1`。

- [ ] **Step 6: 实现聚合与 CLI 并确认 GREEN**

`collect_results()` 的顺序固定为 containers、runtime_contract、local_tcp、database、refresh_log。适配器错误映射为对应固定结果，其他检查继续；绝不把 `str(exc)` 放入详情或输出。`main()` 先序列化再原子发布，只有发布成功后才按总体状态返回。

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
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
        print("aurora_n8n_status=ERROR code=producer_error", file=sys.stderr)
        return 2
```

`collect_results()` 必须使用局部 `try/except` 分别保护 Docker 快照、端口发现、SQLite 和续期状态；适配器异常映射为 `container_inspect_failed`、`runtime_inspect_failed`、`tcp_target_unavailable`、`database_invalid` 或 `refresh_lock_unavailable`，并继续形成五项完整结果。

```python
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
    except (OSError, RuntimeError, subprocess.SubprocessError):
        results["containers"] = CheckResult("FAIL", "container_inspect_failed", {"expected": 4, "running": 0, "restart_counts": {}})
        results["runtime_contract"] = CheckResult("FAIL", "runtime_inspect_failed", {"project_matches": False, "working_dir_matches": False, "mounts_match": False})
    try:
        results["local_tcp"] = check_local_tcp(discover_tcp_targets(run), connect)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        results["local_tcp"] = CheckResult("FAIL", "tcp_target_unavailable", {"expected": 4, "reachable": 0, "services": {name: False for name in sorted(PORT_SPECS)}})
    results["database"] = check_database(root, channel_id, int(now.timestamp()))
    results["refresh_log"] = check_refresh_state(root, now, lock_probe)
    return results
```

Run:

```powershell
python -m unittest tests.test_write_n8n_health_status -v
```

Expected: 新测试文件全部通过。

---

### Task 6: Aurora 文档与本地验证

**Files:**
- Modify: `DESIGN.md`
- Modify: `docs/fnos_deployment.md`
- Verify: `docs/superpowers/specs/2026-08-01-n8n-offline-health-integration-design.md`
- Verify: `docs/superpowers/plans/2026-08-01-n8n-offline-health-producer.md`

**Interfaces:**
- Consumes: Task 5 的最终 CLI、退出码和状态契约。
- Produces: 不超过候选事实的正式文档说明。

- [ ] **Step 1: 更新 DESIGN.md**

在现有 n8n 延期说明附近增加：Aurora 是状态权威；生产器只生成脱敏 Schema v1；Studio OS/n8n 只能消费；当前仍未部署。不得写入“已经接入”“线上验证通过”或任何 Credential/路径以外的敏感运行事实。

- [ ] **Step 2: 更新部署指南**

记录候选命令、退出码、只保留 `latest.json`、05:12/17:12 `Asia/Shanghai` 候选 cron，以及以下明确门禁：当前只完成本地实现；FNOS 现场只读验证、目标目录/cron/只读子挂载部署、n8n 导入、邮件验证和激活均未授权。

- [ ] **Step 3: 运行新测试和现有完整测试**

Run:

```powershell
python -m unittest tests.test_write_n8n_health_status -v
python -m unittest discover -s tests -p "test_*.py" -v
python -m py_compile scripts\write_n8n_health_status.py
```

Expected: 所有测试通过，0 failures，0 errors；py_compile exit 0。

- [ ] **Step 4: 检查 Markdown 相对链接**

Run:

```powershell
$documents = @(
  'DESIGN.md',
  'docs\fnos_deployment.md',
  'docs\superpowers\specs\2026-08-01-n8n-offline-health-integration-design.md',
  'docs\superpowers\plans\2026-08-01-n8n-offline-health-producer.md'
)
foreach ($document in $documents) {
  $base = Split-Path -Parent (Resolve-Path -LiteralPath $document)
  $text = Get-Content -LiteralPath $document -Raw
  foreach ($match in [regex]::Matches($text, '\]\((?<target>[^)]+)\)')) {
    $target = $match.Groups['target'].Value
    if ($target -match '^[a-z]+://' -or $target.StartsWith('#') -or $target.StartsWith('/')) { continue }
    $pathPart = ($target -split '#', 2)[0]
    if ($pathPart -and -not (Test-Path -LiteralPath (Join-Path $base $pathPart))) {
      throw "Missing relative link target in $document"
    }
  }
}
```

Expected: 无输出、无异常。

- [ ] **Step 5: 执行安全与静态边界检查**

Run:

```powershell
rg -n "urllib|http\.client|requests|https?://|chat/completions|/v1/models" scripts\write_n8n_health_status.py
rg -n "[ \t]+$" scripts\write_n8n_health_status.py tests\test_write_n8n_health_status.py DESIGN.md docs\fnos_deployment.md docs\superpowers\specs\2026-08-01-n8n-offline-health-integration-design.md docs\superpowers\plans\2026-08-01-n8n-offline-health-producer.md
git diff --check
```

Expected: 两次 `rg` 均无匹配并以 1 退出；`git diff --check` 无输出并以 0 退出。由于新文件在本门禁保持未跟踪，完成报告还必须说明 `git diff --check` 不覆盖未跟踪文件，行尾空白由第二条 `rg` 单独覆盖。

- [ ] **Step 6: 验证 Compose 静态配置与忽略规则**

Run:

```powershell
$env:NAS_LAN_IP='192.0.2.10'
$env:SESSION_SECRET='non-sensitive-static-check'
$env:COMPOSE_DISABLE_ENV_FILE='true'
docker compose config -q
git check-ignore -v -- .env .secrets/probe data/probe artifacts/probe.fpk TODO.md DEVLOG.md
git status --short
```

Expected: Compose 静态配置 exit 0；六类本地/敏感路径均有忽略规则；状态只显示本计划范围内的新文件和修改，不包含 `.env`、`.secrets/`、`data/`、第三方安装包或 Studio OS 文件。

- [ ] **Step 7: 逐项核对设计验收与最终 diff**

检查每一个变更文件都能追溯到本计划；确认没有修改 `check_stack_health.py`、`refresh_chatgpt_access_token.py` 或 `docker-compose.yml`。完成报告必须列出实际测试数量和结果、未执行的 FNOS/n8n 验证、当前未跟踪/未提交状态，并停在 Aurora 本地实现门禁。

---

## Deferred Work Outside This Plan

- 不创建或修改 `Solis_Studio_OS/workflows/aurora-gateway-alert.workflow.json`。
- 不修改 Studio OS Compose 的覆盖式只读子挂载。
- 不执行 FNOS 现场只读验证、目录创建、文件同步、cron 修改或容器重建。
- 不导入、绑定、测试、发布或激活 n8n 工作流。
- Studio OS 当前维护完成并取得独立批准后，基于本计划产生的最终 Schema 另写工作流实施计划。
