import importlib.util
import base64
import contextlib
import io
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
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("n8n_health_status", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def make_results(status: str = "PASS") -> dict[str, object]:
    return {
        "containers": MODULE.CheckResult(
            status,
            "containers_running",
            {
                "expected": 4,
                "running": 4,
                "restart_counts": {
                    "aurora": 0,
                    "metacubexd": 0,
                    "mihomo": 0,
                    "new-api": 0,
                },
            },
        ),
        "runtime_contract": MODULE.CheckResult(
            "PASS",
            "runtime_matches",
            {
                "project_matches": True,
                "working_dir_matches": True,
                "mounts_match": True,
            },
        ),
        "local_tcp": MODULE.CheckResult(
            "PASS",
            "local_ports_reachable",
            {
                "expected": 4,
                "reachable": 4,
                "services": {
                    "aurora": True,
                    "metacubexd": True,
                    "mihomo": True,
                    "new-api": True,
                },
            },
        ),
        "database": MODULE.CheckResult(
            "PASS",
            "database_and_service_key_valid",
            {
                "integrity_ok": True,
                "channel_active": True,
                "channel_base_matches": True,
                "service_key_matches": True,
            },
        ),
        "refresh_log": MODULE.CheckResult(
            "PASS",
            "refresh_not_applicable",
            {
                "event": "not_applicable",
                "valid_records": 0,
                "invalid_records": 0,
                "event_at": "2026-08-01T09:12:00Z",
            },
        ),
    }


class DocumentTests(unittest.TestCase):
    def test_overall_status_uses_fail_then_warn_then_pass(self):
        self.assertEqual(MODULE.overall_status(make_results()), "PASS")
        warning = make_results()
        warning["database"] = MODULE.CheckResult(
            "WARN",
            "database_invalid",
            {
                "integrity_ok": True,
                "channel_active": True,
                "channel_base_matches": True,
                "service_key_matches": False,
            },
        )
        self.assertEqual(MODULE.overall_status(warning), "WARN")
        warning["containers"] = MODULE.CheckResult(
            "FAIL", "container_state_invalid", {"expected": 4, "running": 3}
        )
        self.assertEqual(MODULE.overall_status(warning), "FAIL")

    def test_document_has_exact_schema_and_deterministic_bytes(self):
        now = datetime(2026, 8, 1, 9, 12, tzinfo=timezone.utc)
        document = MODULE.build_document(make_results(), now)
        self.assertEqual(
            set(document),
            {"schema_version", "producer", "generated_at", "overall", "checks"},
        )
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

    def test_unapproved_detail_keys_and_types_fail_closed(self):
        results = make_results()
        results["database"] = MODULE.CheckResult(
            "PASS",
            "database_and_service_key_valid",
            {
                "integrity_ok": True,
                "channel_active": True,
                "channel_base_matches": True,
                "service_key_matches": True,
                "raw": "private-text",
            },
        )
        with self.assertRaises(ValueError):
            MODULE.build_document(results, datetime.now(timezone.utc))

    def test_service_key_database_shape_is_strictly_code_specific(self):
        service_details = {
            "integrity_ok": True,
            "channel_active": True,
            "channel_base_matches": True,
            "service_key_matches": True,
        }
        results = make_results()
        results["database"] = MODULE.CheckResult(
            "PASS", "database_and_service_key_valid", service_details
        )
        document = MODULE.build_document(results, datetime.now(timezone.utc))
        self.assertEqual(
            document["checks"]["database"],
            {
                "status": "PASS",
                "code": "database_and_service_key_valid",
                "details": service_details,
            },
        )

        results["database"] = MODULE.CheckResult(
            "PASS", "database_and_legacy_token_valid", service_details
        )
        with self.assertRaises(ValueError):
            MODULE.build_document(results, datetime.now(timezone.utc))

        results = make_results()
        results["refresh_log"] = MODULE.CheckResult(
            "PASS",
            "refresh_not_applicable",
            {
                "event": "not_applicable",
                "valid_records": 0,
                "invalid_records": 0,
                "event_at": "2026-08-04T06:10:25Z",
            },
        )
        document = MODULE.build_document(results, datetime.now(timezone.utc))
        self.assertEqual(
            document["checks"]["refresh_log"]["code"],
            "refresh_not_applicable",
        )

        results["refresh_log"] = MODULE.CheckResult(
            "PASS",
            "refresh_recent",
            {
                "event": "not_applicable",
                "valid_records": 0,
                "invalid_records": 0,
                "event_at": "2026-08-04T06:10:25Z",
            },
        )
        with self.assertRaises(ValueError):
            MODULE.build_document(results, datetime.now(timezone.utc))

        results["database"] = MODULE.CheckResult(
            "PASS",
            "database_and_service_key_valid",
            {
                "integrity_ok": True,
                "remaining_seconds": 604800,
                "expires_at": "2026-08-08T09:12:00Z",
            },
        )
        with self.assertRaises(ValueError):
            MODULE.build_document(results, datetime.now(timezone.utc))
        results = make_results()
        results["local_tcp"] = MODULE.CheckResult(
            "PASS",
            "local_ports_reachable",
            {"expected": 4, "reachable": "4", "services": {}},
        )
        with self.assertRaises(ValueError):
            MODULE.build_document(results, datetime.now(timezone.utc))


class DockerCheckTests(unittest.TestCase):
    def snapshots(self):
        root = "/vol1/1000/Solis_Aurora_Gateway"
        return {
            "aurora": MODULE.ContainerSnapshot(
                "aurora",
                "running",
                0,
                "aurora-stack",
                root,
                (
                    MODULE.MountSnapshot(
                        "/home/nonroot/session_tokens.txt",
                        f"{root}/.secrets/session_tokens.txt",
                        False,
                    ),
                ),
            ),
            "new-api": MODULE.ContainerSnapshot(
                "new-api",
                "running",
                1,
                "aurora-stack",
                root,
                (MODULE.MountSnapshot("/data", f"{root}/data/new-api", True),),
            ),
            "mihomo": MODULE.ContainerSnapshot(
                "mihomo",
                "running",
                0,
                "aurora-stack",
                root,
                (
                    MODULE.MountSnapshot(
                        "/root/.config/mihomo", f"{root}/data/mihomo", True
                    ),
                ),
            ),
            "metacubexd": MODULE.ContainerSnapshot(
                "metacubexd", "running", 0, "aurora-stack", root, ()
            ),
        }

    def test_running_containers_pass_and_restart_count_is_metadata_only(self):
        result = MODULE.check_containers(self.snapshots())
        self.assertEqual((result.status, result.code), ("PASS", "containers_running"))
        self.assertEqual(result.details["running"], 4)
        self.assertEqual(result.details["restart_counts"]["new-api"], 1)

    def test_missing_or_stopped_container_fails(self):
        snapshots = self.snapshots()
        snapshots["aurora"] = MODULE.ContainerSnapshot(
            "aurora",
            "exited",
            0,
            "aurora-stack",
            "/vol1/1000/Solis_Aurora_Gateway",
            (),
        )
        result = MODULE.check_containers(snapshots)
        self.assertEqual((result.status, result.code), ("FAIL", "container_state_invalid"))

    def test_runtime_contract_requires_exact_project_workdir_and_mounts(self):
        self.assertEqual(MODULE.check_runtime_contract(self.snapshots()).status, "PASS")
        snapshots = self.snapshots()
        snapshots["new-api"] = MODULE.ContainerSnapshot(
            "new-api",
            "running",
            0,
            "wrong",
            "/vol1/1000/Solis_Aurora_Gateway",
            snapshots["new-api"].mounts,
        )
        result = MODULE.check_runtime_contract(snapshots)
        self.assertEqual((result.status, result.code), ("FAIL", "runtime_mismatch"))
        self.assertEqual(
            set(result.details),
            {"project_matches", "working_dir_matches", "mounts_match"},
        )
        snapshots = self.snapshots()
        snapshots["new-api"] = MODULE.ContainerSnapshot(
            "new-api",
            "running",
            0,
            "aurora-stack",
            "/vol1/1000/Solis_Aurora_Gateway",
            snapshots["new-api"].mounts
            + (MODULE.MountSnapshot("/extra", "/unapproved", True),),
        )
        self.assertFalse(MODULE.check_runtime_contract(snapshots).details["mounts_match"])
        snapshots = self.snapshots()
        snapshots["aurora"] = MODULE.ContainerSnapshot(
            "aurora",
            "running",
            0,
            "aurora-stack",
            "/vol1/1000/Solis_Aurora_Gateway",
            (
                MODULE.MountSnapshot(
                    "/home/nonroot/session_tokens.txt",
                    "/vol1/1000/Solis_Aurora_Gateway/.secrets/session_tokens.txt",
                    True,
                ),
            ),
        )
        self.assertFalse(MODULE.check_runtime_contract(snapshots).details["mounts_match"])

    def test_collect_snapshots_uses_only_bounded_inspect_formats(self):
        calls = []
        outputs = iter(
            [
                "/aurora\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
                "/home/nonroot/session_tokens.txt\t/vol1/1000/Solis_Aurora_Gateway/.secrets/session_tokens.txt\tfalse\n",
                "/new-api\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
                "/data\t/vol1/1000/Solis_Aurora_Gateway/data/new-api\ttrue\n",
                "/mihomo\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
                "/root/.config/mihomo\t/vol1/1000/Solis_Aurora_Gateway/data/mihomo\ttrue\n",
                "/metacubexd\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
                "",
            ]
        )

        def fake_run(args, timeout=20):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, next(outputs), "")

        snapshots = MODULE.collect_container_snapshots(fake_run)
        self.assertEqual(set(snapshots), {"aurora", "new-api", "mihomo", "metacubexd"})
        rendered = " ".join(" ".join(call) for call in calls)
        self.assertNotIn("Config.Env", rendered)
        self.assertNotIn("json .", rendered)
        self.assertNotIn("project.config_files", rendered)

    def test_collect_snapshots_accepts_docker_cli_trailing_blank_lines(self):
        outputs = iter(
            [
                "/aurora\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
                "/home/nonroot/session_tokens.txt\t/vol1/1000/Solis_Aurora_Gateway/.secrets/session_tokens.txt\tfalse\n\n",
                "/new-api\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
                "/data\t/vol1/1000/Solis_Aurora_Gateway/data/new-api\ttrue\n\n",
                "/mihomo\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
                "/root/.config/mihomo\t/vol1/1000/Solis_Aurora_Gateway/data/mihomo\ttrue\n\n",
                "/metacubexd\trunning\t0\taurora-stack\t/vol1/1000/Solis_Aurora_Gateway\n",
                "\n",
            ]
        )

        def fake_run(args, timeout=20):
            return subprocess.CompletedProcess(args, 0, next(outputs), "")

        snapshots = MODULE.collect_container_snapshots(fake_run)

        self.assertEqual(len(snapshots["aurora"].mounts), 1)
        self.assertFalse(snapshots["aurora"].mounts[0].read_write)
        self.assertEqual(len(snapshots["new-api"].mounts), 1)
        self.assertEqual(len(snapshots["mihomo"].mounts), 1)
        self.assertEqual(snapshots["metacubexd"].mounts, ())

    def test_malformed_inspect_fails_with_fixed_error(self):
        def fake_run(args, timeout=20):
            return subprocess.CompletedProcess(args, 0, "raw-private-output", "")

        with self.assertRaisesRegex(RuntimeError, "^container_inspect_failed$"):
            MODULE.collect_container_snapshots(fake_run)


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
        self.assertEqual(
            result.details,
            {
                "expected": 4,
                "reachable": 4,
                "services": {
                    "aurora": True,
                    "metacubexd": True,
                    "mihomo": True,
                    "new-api": True,
                },
            },
        )
        self.assertTrue(all(timeout == 2 for _, timeout in calls))

    def test_unreachable_target_fails_without_exposing_address(self):
        targets = {
            name: ("127.0.0.1", port)
            for name, port in {
                "aurora": 8080,
                "new-api": 3000,
                "metacubexd": 9097,
                "mihomo": 9090,
            }.items()
        }

        def connect(target, timeout):
            if target[1] == 3000:
                raise OSError("sensitive raw socket error")
            return mock.MagicMock()

        result = MODULE.check_local_tcp(targets, connect)
        self.assertEqual((result.status, result.code), ("FAIL", "local_port_unreachable"))
        self.assertNotIn("127.0.0.1", json.dumps(result.details))
        self.assertNotIn("sensitive", json.dumps(result.details))

    def test_port_discovery_normalizes_wildcard_and_rejects_invalid_binding(self):
        outputs = iter(
            [
                "0.0.0.0:8080\n",
                "[::]:3000\n",
                "127.0.0.1:9097\n",
                "192.0.2.10:9090\n",
                "172.18.0.5\n",
            ]
        )

        def fake_run(args, timeout=10):
            return subprocess.CompletedProcess(args, 0, next(outputs), "")

        targets = MODULE.discover_tcp_targets(fake_run)
        self.assertEqual(targets["aurora"], ("127.0.0.1", 8080))
        self.assertEqual(targets["new-api"], ("::1", 3000))
        self.assertEqual(targets["mihomo"], ("172.18.0.5", 9090))

        def invalid_run(args, timeout=10):
            return subprocess.CompletedProcess(args, 0, "not-an-ip:99999\n", "")

        with self.assertRaisesRegex(RuntimeError, "^tcp_target_unavailable$"):
            MODULE.discover_tcp_targets(invalid_run)

    def test_mihomo_uses_bridge_ip_after_validating_published_binding(self):
        calls = []

        def fake_run(args, timeout=10):
            calls.append(args)
            if args[:2] == ["docker", "port"]:
                published = {
                    "aurora": "0.0.0.0:8080\n",
                    "new-api": "0.0.0.0:3000\n",
                    "metacubexd": "0.0.0.0:9097\n",
                    "mihomo": "192.0.2.10:9090\n",
                }
                return subprocess.CompletedProcess(args, 0, published[args[2]], "")
            if args[:2] == ["docker", "inspect"] and args[-1] == "mihomo":
                return subprocess.CompletedProcess(args, 0, "172.18.0.5\n", "")
            raise AssertionError(f"unexpected command: {args}")

        targets = MODULE.discover_tcp_targets(fake_run)

        self.assertEqual(targets["mihomo"], ("172.18.0.5", 9090))
        self.assertIn(
            ["docker", "port", "mihomo", "9090/tcp"],
            calls,
        )


def make_token(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.signature"


class DatabaseCheckTests(unittest.TestCase):
    def test_database_requires_final_service_key_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data" / "new-api" / "one-api.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE channels (id INTEGER PRIMARY KEY, status INTEGER, key TEXT, base_url TEXT)"
                )
                connection.execute(
                    "INSERT INTO channels VALUES (1, 1, ?, ?)",
                    ("service-key-value", "http://aurora:8080"),
                )
                connection.commit()
            finally:
                connection.close()
            (root / ".env").write_text(
                "AURORA_AUTHORIZATION=service-key-value\n", encoding="utf-8"
            )
            self.assertEqual(MODULE.check_database(root, 1, 100000).status, "PASS")

    def test_service_key_channel_passes_with_only_sanitized_booleans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data" / "new-api" / "one-api.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE channels (id INTEGER PRIMARY KEY, status INTEGER, key TEXT, base_url TEXT)"
                )
                connection.execute(
                    "INSERT INTO channels VALUES (1, 1, ?, ?)",
                    (
                        "service-key-value",
                        "http://aurora:8080",
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            (root / ".env").write_text(
                "AURORA_AUTHORIZATION=service-key-value\n",
                encoding="utf-8",
            )

            result = MODULE.check_database(root, 1, 100000)

        self.assertEqual(
            (result.status, result.code),
            ("PASS", "database_and_service_key_valid"),
        )
        self.assertEqual(
            result.details,
            {
                "integrity_ok": True,
                "channel_active": True,
                "channel_base_matches": True,
                "service_key_matches": True,
            },
        )
        self.assertNotIn("service-key-value", json.dumps(result.details))

    def test_service_key_mismatch_is_sanitized_database_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data" / "new-api" / "one-api.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE channels (id INTEGER PRIMARY KEY, status INTEGER, key TEXT, base_url TEXT)"
                )
                connection.execute(
                    "INSERT INTO channels VALUES (1, 1, ?, ?)",
                    (
                        "database-service-key",
                        "http://aurora:8080",
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            (root / ".env").write_text(
                "AURORA_AUTHORIZATION=different-service-key\n",
                encoding="utf-8",
            )

            result = MODULE.check_database(root, 1, 100000)

        self.assertEqual((result.status, result.code), ("FAIL", "database_invalid"))
        self.assertEqual(
            result.details,
            {
                "integrity_ok": True,
                "channel_active": True,
                "channel_base_matches": True,
                "service_key_matches": False,
            },
        )
        rendered = json.dumps(result.details)
        self.assertNotIn("database-service-key", rendered)
        self.assertNotIn("different-service-key", rendered)

    def test_invalid_database_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            result = MODULE.check_database(Path(directory), 1, 100000)
        self.assertEqual((result.status, result.code), ("FAIL", "database_invalid"))
        self.assertEqual(
            set(result.details),
            {
                "integrity_ok",
                "channel_active",
                "channel_base_matches",
                "service_key_matches",
            },
        )


class CollectionTests(unittest.TestCase):
    def test_service_key_mode_publishes_internal_renewal_status(self):
        database_result = MODULE.CheckResult(
            "PASS",
            "database_and_service_key_valid",
            {
                "integrity_ok": True,
                "channel_active": True,
                "channel_base_matches": True,
                "service_key_matches": True,
            },
        )

        def broken_run(args, timeout=20):
            raise RuntimeError("sanitized")

        now = datetime(2026, 8, 4, 6, 10, 25, tzinfo=timezone.utc)
        with mock.patch.object(
            MODULE, "check_database", return_value=database_result
        ):
            results = MODULE.collect_results(
                Path("/candidate"),
                1,
                now,
                run=broken_run,
            )

        self.assertEqual(
            results["refresh_log"],
            MODULE.CheckResult(
                "PASS",
                "refresh_not_applicable",
                {
                    "event": "not_applicable",
                    "valid_records": 0,
                    "invalid_records": 0,
                    "event_at": "2026-08-04T06:10:25Z",
                },
            ),
        )

    def test_adapter_failures_are_sanitized_and_other_checks_continue(self):
        database_result = MODULE.CheckResult(
            "PASS",
            "database_and_service_key_valid",
            {
                "integrity_ok": True,
                "channel_active": True,
                "channel_base_matches": True,
                "service_key_matches": True,
            },
        )

        def broken_run(args, timeout=20):
            raise RuntimeError("raw-private-command-error")

        with mock.patch.object(
            MODULE, "check_database", return_value=database_result
        ) as database:
            results = MODULE.collect_results(
                Path("/candidate"),
                1,
                datetime(2026, 8, 1, 9, 12, tzinfo=timezone.utc),
                run=broken_run,
            )
        self.assertEqual(tuple(results), MODULE.EXPECTED_CHECKS)
        self.assertEqual(results["containers"].code, "container_inspect_failed")
        self.assertEqual(results["runtime_contract"].code, "runtime_inspect_failed")
        self.assertEqual(results["local_tcp"].code, "tcp_target_unavailable")
        self.assertNotIn("raw-private", json.dumps({k: vars(v) for k, v in results.items()}))
        database.assert_called_once()


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

    def test_write_failure_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            output.write_bytes(b"old\n")
            with mock.patch.object(MODULE.os, "replace", side_effect=OSError("private")):
                with self.assertRaises(OSError):
                    MODULE.atomic_write(output, b"new\n")
            self.assertEqual(output.read_bytes(), b"old\n")
            self.assertEqual([path.name for path in output.parent.iterdir()], ["latest.json"])

    def test_symlink_parent_branch_fails_closed_without_platform_support(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            with mock.patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaises(ValueError):
                    MODULE.atomic_write(output, b"{}\n")


class CliTests(unittest.TestCase):
    def test_pass_and_fail_exit_codes_publish_once(self):
        cases = (("PASS", 0), ("FAIL", 1))
        for status_value, expected_code in cases:
            with self.subTest(status=status_value), tempfile.TemporaryDirectory() as directory:
                results = make_results()
                if status_value != "PASS":
                    results["database"] = MODULE.CheckResult(
                        status_value,
                        "database_invalid",
                        {
                            "integrity_ok": True,
                            "channel_active": True,
                            "channel_base_matches": True,
                            "service_key_matches": False,
                        },
                    )
                stdout = io.StringIO()
                with (
                    mock.patch.object(MODULE, "collect_results", return_value=results),
                    mock.patch.object(MODULE, "atomic_write") as write,
                    contextlib.redirect_stdout(stdout),
                ):
                    exit_code = MODULE.main(
                        ["--output", str(Path(directory) / "latest.json")]
                    )
                self.assertEqual(exit_code, expected_code)
                self.assertEqual(
                    stdout.getvalue(),
                    f"aurora_n8n_status={status_value} code=published\n",
                )
                write.assert_called_once()

    def test_producer_error_is_fixed_and_sanitized(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(
                MODULE,
                "collect_results",
                side_effect=RuntimeError("raw-private-exception"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = MODULE.main(["--output", "latest.json"])
        self.assertEqual(exit_code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "aurora_n8n_status=ERROR code=producer_error\n",
        )
        self.assertNotIn("private", stderr.getvalue())

    def test_parse_defaults_and_required_output(self):
        args = MODULE.parse_args(["--output", "latest.json"])
        self.assertEqual(args.channel_id, 1)
        self.assertEqual(args.root, SCRIPT.resolve().parents[1])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                MODULE.parse_args([])
        self.assertEqual(raised.exception.code, 2)
        results = make_results()
        results["containers"] = MODULE.CheckResult(
            "PASS", "unapproved_code", {"expected": 4, "running": 4}
        )
        with self.assertRaises(ValueError):
            MODULE.build_document(results, datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
