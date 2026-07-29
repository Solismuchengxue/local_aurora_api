import base64
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "check_stack_health.py"
)
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("stack_health", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ResultTests(unittest.TestCase):
    def test_build_report_preserves_detail_keys_when_redacting_values(self):
        first_secret = "first-secret"
        second_secret = "second-secret"
        first_key = f"credential-{first_secret}"
        second_key = f"credential-{second_secret}"
        report = MODULE.build_report(
            [
                MODULE.CheckResult(
                    "containers",
                    "PASS",
                    "ok",
                    {first_key: first_secret, second_key: second_secret},
                )
            ],
            "2026-07-29T12:00:00+08:00",
            secrets=(first_secret, second_secret),
        )

        details = report["checks"][0]["details"]
        self.assertEqual(set(details), {first_key, second_key})
        self.assertEqual(details[first_key], "[redacted]")
        self.assertEqual(details[second_key], "[redacted]")

    def test_build_report_preserves_long_text_without_secrets(self):
        value = "x" * 241 + "\nunchanged"
        report = MODULE.build_report(
            [
                MODULE.CheckResult(
                    "containers",
                    "PASS",
                    value,
                    {"message": value},
                )
            ],
            "2026-07-29T12:00:00+08:00",
        )

        check = report["checks"][0]
        self.assertEqual(check["summary"], value)
        self.assertEqual(check["details"]["message"], value)

    def test_check_result_rejects_unknown_status(self):
        with self.assertRaises(ValueError) as error:
            MODULE.CheckResult("unknown", "UNKNOWN", "invalid", {})
        self.assertEqual(str(error.exception), "unknown check status")

    def test_build_report_redacts_known_secrets_before_rendering(self):
        secret = "test-token-value"
        results = [
            MODULE.CheckResult(
                "containers",
                "PASS",
                f"容器令牌为 {secret}",
                {
                    "token": secret,
                    "nested": [
                        {"message": f"嵌套令牌为 {secret}"},
                        (secret,),
                    ],
                },
            )
        ]
        report = MODULE.build_report(
            results,
            "2026-07-29T12:00:00+08:00",
            secrets=(secret,),
        )

        self.assertNotIn(secret, repr(report))
        self.assertNotIn(secret, MODULE.render_json(report))
        self.assertNotIn(secret, MODULE.render_human(report))
        self.assertEqual(results[0].summary, f"容器令牌为 {secret}")
        self.assertEqual(results[0].details["token"], secret)

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

    def test_top_level_object_fails_without_raising(self):
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"Name": "/aurora"}),
                stderr="",
            )
        )
        result = MODULE.check_containers(run)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details["error"], "docker_inspect_error")

    def test_malformed_state_fails_without_raising(self):
        payload = [
            {
                "Name": "/aurora",
                "State": "running",
                "RestartCount": 0,
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
        self.assertEqual(result.details["error"], "docker_inspect_error")


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
        self.assertEqual(client_token, "sk-client-token-value")
        self.assertIn(self.channel_token, secrets)
        self.assertIn("client-token-value", secrets)
        self.assertIn("sk-client-token-value", secrets)
        serialized = json.dumps(result.details)
        self.assertNotIn(self.channel_token, serialized)
        self.assertNotIn("client-token-value", serialized)
        self.assertNotIn("sk-client-token-value", serialized)

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

    def test_json_scalar_or_array_warns_when_valid_event_exists(self):
        self.log.write_text(
            '"scalar"\n'
            + "[]\n"
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
        self.assertEqual(result.details["invalid_lines"], 2)

    def test_unknown_and_untrusted_log_values_are_not_echoed(self):
        secret = "known-secret"
        unknown_event = "internal_failure"
        invalid_time = "not-a-timestamp"
        string_number = "123"
        self.log.write_text(
            json.dumps(
                {
                    "time": invalid_time,
                    "event": unknown_event,
                    "reason": f"request failed {secret}",
                    "remaining_seconds": string_number,
                    "channel_id": True,
                    "previous_exp": string_number,
                    "new_exp": True,
                    "extension_seconds": string_number,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = MODULE.check_refresh_log(self.root, (secret,))
        serialized = json.dumps(
            {"summary": result.summary, "details": result.details}
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.summary, "未知续期事件")
        self.assertEqual(result.details["event"], "unknown")
        self.assertNotIn(secret, serialized)
        self.assertNotIn(unknown_event, serialized)
        self.assertNotIn(invalid_time, serialized)
        self.assertNotIn(string_number, serialized)
        self.assertNotIn("reason", result.details)

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

    def test_docker_inspect_timeout_returns_fixed_failure(self):
        run = mock.Mock(
            side_effect=subprocess.TimeoutExpired(["docker", "inspect"], 15)
        )
        result = MODULE.check_mihomo(run=run)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details, {"error": "mihomo_check_failed"})

    def test_non_object_config_returns_fixed_failure(self):
        fetch_json = mock.Mock(side_effect=[[], {"now": "Singapore Node"}])
        with mock.patch.object(
            MODULE,
            "discover_mihomo_endpoints",
            return_value=(
                "http://172.19.0.3:9090",
                "http://172.19.0.3:7890",
            ),
        ):
            result = MODULE.check_mihomo(
                fetch_json,
                mock.Mock(return_value="loc=SG\n"),
            )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details, {"error": "mihomo_check_failed"})

    def test_non_object_global_proxy_returns_fixed_failure(self):
        fetch_json = mock.Mock(side_effect=[{"mode": "global"}, []])
        with mock.patch.object(
            MODULE,
            "discover_mihomo_endpoints",
            return_value=(
                "http://172.19.0.3:9090",
                "http://172.19.0.3:7890",
            ),
        ):
            result = MODULE.check_mihomo(
                fetch_json,
                mock.Mock(return_value="loc=SG\n"),
            )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details, {"error": "mihomo_check_failed"})

    def test_non_text_trace_returns_fixed_failure(self):
        fetch_json = mock.Mock(
            side_effect=[
                {"mode": "global"},
                {"now": "Singapore Node"},
            ]
        )
        with mock.patch.object(
            MODULE,
            "discover_mihomo_endpoints",
            return_value=(
                "http://172.19.0.3:9090",
                "http://172.19.0.3:7890",
            ),
        ):
            result = MODULE.check_mihomo(fetch_json, mock.Mock(return_value=1))
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details, {"error": "mihomo_check_failed"})


class StrictHttpTests(unittest.TestCase):
    def make_urlopen(self, status, body):
        response = mock.MagicMock()
        response.status = status
        response.read.return_value = body
        urlopen = mock.MagicMock()
        urlopen.return_value.__enter__.return_value = response
        return urlopen

    def test_request_json_200_rejects_created_response(self):
        urlopen = self.make_urlopen(
            201,
            json.dumps(
                {"choices": [{"message": {"content": "OK"}}]}
            ).encode(),
        )
        with mock.patch.object(MODULE.urllib.request, "urlopen", urlopen):
            with self.assertRaises(MODULE.token_refresh.RefreshError):
                MODULE.request_json_200(
                    "http://127.0.0.1:3000/v1/chat/completions",
                    "client-secret",
                    payload={"model": "gpt-5-6-pro"},
                )

    def test_request_json_200_accepts_object_response(self):
        expected = {"data": [{"id": "gpt-5-6-pro"}]}
        urlopen = self.make_urlopen(200, json.dumps(expected).encode())
        with mock.patch.object(MODULE.urllib.request, "urlopen", urlopen):
            result = MODULE.request_json_200(
                "http://127.0.0.1:3000/v1/models",
                "client-secret",
            )
        self.assertEqual(result, expected)

    def test_request_json_200_rejects_non_object_json(self):
        urlopen = self.make_urlopen(200, b"[]")
        with mock.patch.object(MODULE.urllib.request, "urlopen", urlopen):
            with self.assertRaises(MODULE.token_refresh.RefreshError):
                MODULE.request_json_200(
                    "http://127.0.0.1:3000/v1/models",
                    "client-secret",
                )

    def test_default_chat_request_rejects_created_completion(self):
        urlopen = self.make_urlopen(
            201,
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "OK"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ).encode(),
        )
        with mock.patch.object(MODULE.urllib.request, "urlopen", urlopen):
            result = MODULE.check_chat("client-secret")
        self.assertEqual(result.status, "FAIL")


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


class CliTests(unittest.TestCase):
    def test_run_health_check_collects_all_six_checks(self):
        secret = "client-secret"

        def pass_result(name: str) -> MODULE.CheckResult:
            return MODULE.CheckResult(
                name,
                "PASS",
                f"ok {secret}",
                {"nested": {"secret": secret}},
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
                    secret,
                    ("channel-secret", secret),
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
        self.assertNotIn(secret, serialized)

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


if __name__ == "__main__":
    unittest.main()
