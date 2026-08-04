import http.client
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
        projected = "\n".join(
            [
                "/aurora\trunning\t0",
                "/new-api\trunning\t0",
                "/mihomo\trunning\t0",
                "/metacubexd\trunning\t0",
            ]
        )
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=projected,
                stderr="sensitive diagnostic must be ignored",
            )
        )
        result = MODULE.check_containers(run)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.details["running"], 4)
        run.assert_called_once_with(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Name}}\t{{.State.Status}}\t{{.RestartCount}}",
                "aurora",
                "new-api",
                "mihomo",
                "metacubexd",
            ],
            timeout=20,
        )
        self.assertNotIn("sensitive diagnostic", json.dumps(result.details))

    def test_stopped_or_restarted_container_fails(self):
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout="/aurora\texited\t1",
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
                stdout='{"Name": "/aurora"}',
                stderr="",
            )
        )
        result = MODULE.check_containers(run)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details["error"], "docker_inspect_error")

    def test_malformed_state_fails_without_raising(self):
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout="/aurora\trunning\tnot-an-integer",
                stderr="",
            )
        )
        result = MODULE.check_containers(run)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details["error"], "docker_inspect_error")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        db_dir = self.root / "data" / "new-api"
        db_dir.mkdir(parents=True)
        self.database = db_dir / "one-api.db"
        self.now = 1_700_000_000
        self.channel_key = "aurora-service-key"
        (self.root / ".env").write_text(
            f"AURORA_AUTHORIZATION={self.channel_key}\n", encoding="utf-8"
        )
        with sqlite3.connect(self.database) as database:
            database.execute(
                "CREATE TABLE channels "
                "(id INTEGER PRIMARY KEY, key TEXT, status INTEGER, base_url TEXT)"
            )
            database.execute(
                "CREATE TABLE tokens "
                "(id INTEGER PRIMARY KEY, key TEXT, status INTEGER, "
                "expired_time INTEGER, unlimited_quota INTEGER, "
                "remain_quota INTEGER)"
            )
            database.execute(
                "INSERT INTO channels VALUES (1, ?, 1, ?)",
                (self.channel_key, "http://aurora:8080"),
            )
            database.execute(
                "INSERT INTO tokens VALUES (1, ?, 1, -1, 1, 0)",
                ("client-token-value",),
            )
        database.close()

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
        self.assertIn(self.channel_key, secrets)
        self.assertIn("client-token-value", secrets)
        self.assertIn("sk-client-token-value", secrets)
        serialized = json.dumps(result.details)
        self.assertNotIn(self.channel_key, serialized)
        self.assertNotIn("client-token-value", serialized)
        self.assertNotIn("sk-client-token-value", serialized)

    def test_wrong_service_key_fails(self):
        with sqlite3.connect(self.database) as database:
            database.execute(
                "UPDATE channels SET key = ? WHERE id = 1",
                ("different-service-key",),
            )
        database.close()
        result, _, _ = MODULE.check_database(self.root, 1, self.now)
        self.assertEqual(result.status, "FAIL")

    def test_wrong_base_url_fails(self):
        with sqlite3.connect(self.database) as database:
            database.execute(
                "UPDATE channels SET base_url = ? WHERE id = 1",
                ("http://legacy:8080",),
            )
        database.close()
        result, _, _ = MODULE.check_database(self.root, 1, self.now)
        self.assertEqual(result.status, "FAIL")

    def assert_invalid_client_token(self, value):
        with sqlite3.connect(self.database) as database:
            database.execute(
                "UPDATE tokens SET key = ? WHERE id = 1",
                (value,),
            )
        database.close()
        result, client_token, secrets = MODULE.check_database(
            self.root,
            1,
            self.now,
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIsNone(client_token)
        self.assertEqual(secrets, (self.channel_key,))
        serialized = json.dumps(
            {"summary": result.summary, "details": result.details}
        )
        self.assertNotIn("sk-None", serialized)
        self.assertNotIn('"sk-"', serialized)

    def test_null_client_token_fails_without_normalizing_it(self):
        self.assert_invalid_client_token(None)

    def test_empty_client_token_fails_without_normalizing_it(self):
        self.assert_invalid_client_token("")

    def test_non_string_client_token_fails_without_normalizing_it(self):
        self.assert_invalid_client_token(sqlite3.Binary(b"binary-client-token"))

    def test_integrity_check_failure_blocks_tokens(self):
        real_database = sqlite3.connect(self.database)

        class IntegrityFailureConnection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                real_database.close()

            def close(self):
                real_database.close()

            def execute(self, statement, parameters=()):
                if statement == "PRAGMA integrity_check":
                    cursor = mock.Mock()
                    cursor.fetchone.return_value = ("database malformed",)
                    return cursor
                return real_database.execute(statement, parameters)

        with mock.patch.object(
            MODULE.sqlite3,
            "connect",
            return_value=IntegrityFailureConnection(),
        ):
            result, client_token, secrets = MODULE.check_database(
                self.root,
                1,
                self.now,
            )

        self.assertEqual(result.status, "FAIL")
        self.assertIsNone(client_token)
        self.assertEqual(secrets, (self.channel_key,))
        self.assertEqual(
            result.details,
            {"error": "database_state_invalid"},
        )


class RefreshLogTests(unittest.TestCase):
    def test_external_refresh_log_is_not_applicable(self):
        result = MODULE.check_refresh_log(Path("/not-read"))

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            result.details,
            {"mode": "aurora_internal", "external_refresh": "not_applicable"},
        )


class MihomoTests(unittest.TestCase):
    def run_check(self, config, global_proxy, trace):
        with mock.patch.object(
            MODULE,
            "discover_mihomo_endpoints",
            return_value=(
                "http://172.19.0.3:9090",
                "http://172.19.0.3:7890",
            ),
        ):
            return MODULE.check_mihomo(
                mock.Mock(side_effect=[config, global_proxy]),
                mock.Mock(return_value=trace),
            )

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

    def test_external_fields_must_be_bounded_strings(self):
        raw_fragment = "UNTRUSTED-FRAGMENT"
        cases = [
            (
                {"mode": {"nested": raw_fragment}},
                {"now": "Singapore Node"},
                "loc=SG\n",
            ),
            (
                {"mode": "global"},
                {"now": {"nested": raw_fragment}},
                "loc=SG\n",
            ),
            (
                {"mode": "global"},
                {"now": raw_fragment * 20},
                "loc=SG\n",
            ),
            (
                {"mode": "global"},
                {"now": "Singapore Node"},
                f"loc={raw_fragment * 20}\n",
            ),
        ]
        for config, global_proxy, trace in cases:
            with self.subTest(
                config=config,
                global_proxy=global_proxy,
                trace_length=len(trace),
            ):
                result = self.run_check(config, global_proxy, trace)
                serialized = json.dumps(
                    {"summary": result.summary, "details": result.details}
                )
                self.assertEqual(result.status, "FAIL")
                self.assertEqual(
                    result.details,
                    {"error": "mihomo_check_failed"},
                )
                self.assertNotIn(raw_fragment, serialized)

    def test_control_incomplete_read_returns_fixed_failure(self):
        raw_fragment = b"partial-control-fragment"
        response = mock.MagicMock()
        response.read.side_effect = http.client.IncompleteRead(
            raw_fragment,
            20,
        )
        urlopen = mock.MagicMock()
        urlopen.return_value.__enter__.return_value = response
        with (
            mock.patch.object(
                MODULE,
                "discover_mihomo_endpoints",
                return_value=(
                    "http://172.19.0.3:9090",
                    "http://172.19.0.3:7890",
                ),
            ),
            mock.patch.object(MODULE.urllib.request, "urlopen", urlopen),
        ):
            result = MODULE.check_mihomo()
        serialized = json.dumps(
            {"summary": result.summary, "details": result.details}
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details, {"error": "mihomo_check_failed"})
        self.assertNotIn(raw_fragment.decode(), serialized)

    def test_proxy_incomplete_read_returns_fixed_failure(self):
        raw_fragment = b"partial-proxy-fragment"
        response = mock.MagicMock()
        response.read.side_effect = http.client.IncompleteRead(
            raw_fragment,
            20,
        )
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with (
            mock.patch.object(
                MODULE,
                "discover_mihomo_endpoints",
                return_value=(
                    "http://172.19.0.3:9090",
                    "http://172.19.0.3:7890",
                ),
            ),
            mock.patch.object(
                MODULE.urllib.request,
                "build_opener",
                return_value=opener,
            ),
        ):
            result = MODULE.check_mihomo(
                mock.Mock(
                    side_effect=[
                        {"mode": "global"},
                        {"now": "Singapore Node"},
                    ]
                )
            )
        serialized = json.dumps(
            {"summary": result.summary, "details": result.details}
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details, {"error": "mihomo_check_failed"})
        self.assertNotIn(raw_fragment.decode(), serialized)


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
            with self.assertRaises(MODULE.HealthError):
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
            with self.assertRaises(MODULE.HealthError):
                MODULE.request_json_200(
                    "http://127.0.0.1:3000/v1/models",
                    "client-secret",
                )

    def test_request_json_200_translates_incomplete_read(self):
        raw_fragment = b"partial-api-fragment"
        response = mock.MagicMock()
        response.status = 200
        response.read.side_effect = http.client.IncompleteRead(
            raw_fragment,
            20,
        )
        urlopen = mock.MagicMock()
        urlopen.return_value.__enter__.return_value = response
        with mock.patch.object(MODULE.urllib.request, "urlopen", urlopen):
            with self.assertRaises(MODULE.HealthError) as error:
                MODULE.request_json_200(
                    "http://127.0.0.1:3000/v1/models",
                    "client-secret",
                )
        self.assertEqual(str(error.exception), "health API request failed")
        self.assertNotIn(raw_fragment.decode(), str(error.exception))

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

    def test_malformed_or_oversized_model_ids_return_fixed_failure(self):
        raw_fragment = "UNTRUSTED-MODEL-FRAGMENT"
        cases = [
            {"data": {"nested": raw_fragment}},
            {"data": [{"id": {"nested": raw_fragment}}]},
            {"data": [{"id": raw_fragment * 20}]},
            {"data": [{"id": f"model/{raw_fragment}"}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                result = MODULE.check_models(
                    "client-secret",
                    mock.Mock(return_value=payload),
                )
                serialized = json.dumps(
                    {"summary": result.summary, "details": result.details}
                )
                self.assertEqual(result.status, "FAIL")
                self.assertEqual(
                    result.details,
                    {"error": "model_response_invalid"},
                )
                self.assertNotIn(raw_fragment, serialized)

    def test_excessive_model_list_returns_fixed_failure(self):
        payload = {
            "data": [
                {"id": f"model-{index}"}
                for index in range(65)
            ]
        }
        result = MODULE.check_models(
            "client-secret",
            mock.Mock(return_value=payload),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(
            result.details,
            {"error": "model_response_invalid"},
        )
        self.assertNotIn("model-64", json.dumps(result.details))

    def test_incomplete_read_returns_fixed_failure(self):
        raw_fragment = b"partial-model-fragment"
        result = MODULE.check_models(
            "client-secret",
            mock.Mock(
                side_effect=http.client.IncompleteRead(raw_fragment, 20)
            ),
        )
        serialized = json.dumps(
            {"summary": result.summary, "details": result.details}
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(
            result.details,
            {"error": "model_request_failed"},
        )
        self.assertNotIn(raw_fragment.decode(), serialized)


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
                MODULE.HealthError("pro failed"),
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
            side_effect=MODULE.HealthError(
                f"request failed {secret}"
            )
        )
        result = MODULE.check_chat(secret, request)
        self.assertEqual(result.status, "FAIL")
        self.assertNotIn(secret, json.dumps(result.details))
        self.assertNotIn(secret, result.summary)

    def test_incomplete_read_returns_fixed_failure(self):
        raw_fragment = b"partial-chat-fragment"
        result = MODULE.check_chat(
            "client-secret",
            mock.Mock(
                side_effect=http.client.IncompleteRead(raw_fragment, 20)
            ),
        )
        serialized = json.dumps(
            {"summary": result.summary, "details": result.details}
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details, {"attempts_failed": 2})
        self.assertNotIn(raw_fragment.decode(), serialized)


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

    def test_failed_database_blocks_downstream_checks_with_client_token(self):
        def pass_result(name: str) -> MODULE.CheckResult:
            return MODULE.CheckResult(name, "PASS", "ok", {})

        models = mock.Mock(return_value=pass_result("models"))
        chat = mock.Mock(return_value=pass_result("chat"))
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
                    MODULE.CheckResult(
                        "database",
                        "FAIL",
                        "database failed",
                        {},
                    ),
                    "client-secret",
                    ("client-secret",),
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
            mock.patch.object(MODULE, "check_models", models),
            mock.patch.object(MODULE, "check_chat", chat),
        ):
            report = MODULE.run_health_check(
                Path("/example"), 1, now=1_700_000_000
            )

        models.assert_not_called()
        chat.assert_not_called()
        self.assertEqual(
            report["checks"][4],
            {
                "name": "models",
                "status": "FAIL",
                "summary": "缺少前置检查：database",
                "details": {
                    "error": "dependency_failed",
                    "dependency": "database",
                },
            },
        )
        self.assertEqual(
            report["checks"][5],
            {
                "name": "chat",
                "status": "FAIL",
                "summary": "缺少前置检查：database",
                "details": {
                    "error": "dependency_failed",
                    "dependency": "database",
                },
            },
        )

    def test_warn_database_allows_downstream_checks_with_client_token(self):
        def pass_result(name: str) -> MODULE.CheckResult:
            return MODULE.CheckResult(name, "PASS", "ok", {})

        models = mock.Mock(return_value=pass_result("models"))
        chat = mock.Mock(return_value=pass_result("chat"))
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
                    MODULE.CheckResult(
                        "database",
                        "WARN",
                        "database warning",
                        {},
                    ),
                    "client-secret",
                    ("client-secret",),
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
            mock.patch.object(MODULE, "check_models", models),
            mock.patch.object(MODULE, "check_chat", chat),
        ):
            MODULE.run_health_check(Path("/example"), 1, now=1_700_000_000)

        models.assert_called_once_with("client-secret")
        chat.assert_called_once_with("client-secret")

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
