import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "aurora_session_renewal_probe.py"
SPEC = importlib.util.spec_from_file_location("aurora_session_renewal_probe", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

INVALID_ENV_ENTRIES = (
    'AURORA_CANARY_AUTHORIZATION="service-key"\n',
    "AURORA_CANARY_AUTHORIZATION='service-key'\n",
    "AURORA_CANARY_AUTHORIZATION=service-key # comment\n",
    "export AURORA_CANARY_AUTHORIZATION=service-key\n",
    " AURORA_CANARY_AUTHORIZATION=service-key\n",
    "AURORA_CANARY_AUTHORIZATION=service-key \n",
    "AURORA_CANARY_AUTHORIZATION=service key\n",
    "AURORA_CANARY_AUTHORIZATION=service-$VAR\n",
    "AURORA_CANARY_AUTHORIZATION=service-${VAR}\n",
    "AURORA_CANARY_AUTHORIZATION=service-$$\n",
    "AURORA_CANARY_AUTHORIZATION=\n",
    "AURORA_CANARY_AUTHORIZATION=bad\x00value\n",
    "AURORA_CANARY_AUTHORIZATION=one\nAURORA_CANARY_AUTHORIZATION=two\n",
    "AURORA_CANARY_AUTHORIZATION=one\nexport   AURORA_CANARY_AUTHORIZATION=two\n",
)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body

    def read(self, size=-1):
        return self._body if size == -1 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_opener(calls, status, body):
    def opener(request, timeout):
        calls.append((request, timeout))
        return FakeResponse(status, body)

    return opener


def http_error(status, body):
    return HTTPError(MODULE.BASE_URL, status, "synthetic", {}, io.BytesIO(body))


class AuroraSessionRenewalProbeTests(unittest.TestCase):
    def test_probe_makes_exactly_one_request_and_returns_sanitized_pass(self):
        calls = []
        result = MODULE.probe_once(
            "service-key",
            opener=fake_opener(calls, 200, {"choices": [{"message": {"content": "synthetic-ok"}}]}),
        )
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:18082/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer service-key")
        self.assertEqual(timeout, 60)
        self.assertEqual(json.loads(request.data.decode("utf-8")), {
            "model": "gpt-4o",
            "stream": False,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        })
        self.assertEqual(result["classification"], "pass")
        self.assertNotIn("synthetic-ok", json.dumps(result))
        self.assertNotIn("service-key", json.dumps(result))

    def test_probe_classifies_401_as_auth_failed_without_body_output(self):
        def opener(request, timeout):
            raise http_error(401, b'{"error":{"message":"private response"}}')

        result = MODULE.probe_once("service-key", opener=opener)
        self.assertEqual(result["classification"], "auth_failed")
        self.assertNotIn("private response", MODULE.render_json(result))

    def test_probe_classifies_fixed_account_error_as_auth_failed(self):
        def opener(request, timeout):
            raise http_error(500, b'{"error":{"message":"no available account of the requested type"}}')

        self.assertEqual(MODULE.probe_once("service-key", opener=opener)["classification"], "auth_failed")

    def test_probe_reads_no_more_than_four_kib_of_error_body(self):
        class TrackingError(HTTPError):
            def __init__(self):
                super().__init__(MODULE.BASE_URL, 500, "synthetic", {}, io.BytesIO(b"x" * 8192))
                self.read_sizes = []

            def read(self, size=-1):
                self.read_sizes.append(size)
                return self.fp.read(size)

        error = TrackingError()

        def opener(request, timeout):
            raise error

        MODULE.probe_once("service-key", opener=opener)
        self.assertEqual(error.read_sizes, [4 * 1024])

    def test_probe_reads_no_more_than_sixty_four_kib_of_success_body(self):
        class TrackingResponse(FakeResponse):
            def __init__(self):
                super().__init__(200, {"choices": [{"message": {"content": "synthetic-ok"}}]})
                self.read_sizes = []

            def read(self, size=-1):
                self.read_sizes.append(size)
                return super().read(size)

        response = TrackingResponse()

        def opener(request, timeout):
            return response

        MODULE.probe_once("service-key", opener=opener)
        self.assertEqual(response.read_sizes, [64 * 1024])

    def test_default_transport_disables_proxies_and_redirects_before_a_second_target(self):
        request = Request("http://127.0.0.1:18082/v1/chat/completions", headers={"Authorization": "service-key"})
        with patch.object(MODULE, "build_opener", return_value=object()) as build:
            MODULE._build_loopback_opener()
        proxy_handler, redirect_handler = build.call_args.args
        self.assertIsInstance(proxy_handler, ProxyHandler)
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIsNone(redirect_handler.redirect_request(
            request, None, 302, "Found", {}, "http://example.invalid/second-target"
        ))
        with patch.object(MODULE._LOOPBACK_OPENER, "open", return_value=FakeResponse(
            200, {"choices": [{"message": {"content": "synthetic-ok"}}]}
        )) as transport_open:
            MODULE.probe_once("service-key")
        transport_open.assert_called_once()
        self.assertEqual(transport_open.call_args.args[0].full_url, request.full_url)

    def test_probe_classifies_403_as_upstream_forbidden(self):
        def opener(request, timeout):
            raise http_error(403, b'{"error":{"message":"no available account of the requested type"}}')

        self.assertEqual(MODULE.probe_once("service-key", opener=opener)["classification"], "upstream_forbidden")

    def test_probe_classifies_connection_failure_as_unavailable(self):
        def opener(request, timeout):
            raise URLError("synthetic connection failure")

        self.assertEqual(MODULE.probe_once("service-key", opener=opener)["classification"], "unavailable")

    def test_main_maps_success_response_read_transport_failures_to_sanitized_unavailable(self):
        class FailingResponse(FakeResponse):
            def __init__(self, exception):
                super().__init__(200, b"")
                self._exception = exception

            def read(self, size=-1):
                raise self._exception

        for exception in (
            TimeoutError("private timeout detail"),
            ConnectionResetError("private reset detail"),
            OSError("private transport detail"),
        ):
            with self.subTest(exception=type(exception).__name__):
                output = io.StringIO()
                with redirect_stdout(output), \
                        patch.object(MODULE, "read_env_value", return_value="service-key"), \
                        patch.object(MODULE._LOOPBACK_OPENER, "open", return_value=FailingResponse(exception)) as opener:
                    exit_code = MODULE.main(["--allow-real-api", "--json"])
                opener.assert_called_once()
                self.assertNotEqual(exit_code, 0)
                rendered = output.getvalue()
                self.assertEqual(rendered.count("\n"), 1)
                result = json.loads(rendered)
                self.assertEqual(result["classification"], "unavailable")
                self.assertEqual(set(result), set(MODULE._RESULT_FIELDS))
                self.assertNotIn(str(exception), rendered)

    def test_main_maps_http_error_body_read_transport_failures_to_sanitized_unavailable(self):
        class FailingHTTPError(HTTPError):
            def __init__(self, exception):
                super().__init__(MODULE.BASE_URL, 500, "private response", {}, io.BytesIO(b"private body"))
                self._exception = exception

            def read(self, size=-1):
                raise self._exception

        for exception in (
            TimeoutError("private timeout detail"),
            ConnectionResetError("private reset detail"),
            OSError("private transport detail"),
        ):
            with self.subTest(exception=type(exception).__name__):
                error = FailingHTTPError(exception)
                output = io.StringIO()
                with redirect_stdout(output), \
                        patch.object(MODULE, "read_env_value", return_value="service-key"), \
                        patch.object(MODULE._LOOPBACK_OPENER, "open", side_effect=error) as opener:
                    exit_code = MODULE.main(["--allow-real-api", "--json"])
                opener.assert_called_once()
                self.assertNotEqual(exit_code, 0)
                rendered = output.getvalue()
                self.assertEqual(rendered.count("\n"), 1)
                result = json.loads(rendered)
                self.assertEqual(result["classification"], "unavailable")
                self.assertEqual(set(result), set(MODULE._RESULT_FIELDS))
                self.assertNotIn(str(exception), rendered)

    def test_probe_classifies_invalid_json_and_structure_as_invalid_response(self):
        invalid_json = MODULE.probe_once("service-key", opener=fake_opener([], 200, b"not-json"))
        invalid_structure = MODULE.probe_once("service-key", opener=fake_opener([], 200, {"choices": []}))
        self.assertEqual(invalid_json["classification"], "invalid_response")
        self.assertFalse(invalid_json["response_is_json"])
        self.assertEqual(invalid_structure["classification"], "invalid_response")

    def test_render_json_only_emits_allowlisted_fields(self):
        rendered = MODULE.render_json({"classification": "pass", "http_status": 200, "response_is_json": True,
                                       "choices_present": True, "message_present": True, "content_nonempty": True,
                                       "private": "must-not-appear"})
        self.assertEqual(set(json.loads(rendered)), {"classification", "http_status", "response_is_json", "choices_present", "message_present", "content_nonempty"})

    def test_main_fails_closed_without_allow_real_api(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(MODULE, "probe_once") as probe, \
                patch.object(MODULE, "read_env_value") as read_env_value:
            exit_code = MODULE.main([])
        self.assertNotEqual(exit_code, 0)
        probe.assert_not_called()
        read_env_value.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_main_accepts_exact_task_5_arguments_and_makes_one_sanitized_request(self):
        output = io.StringIO()
        with redirect_stdout(output), \
                patch.object(MODULE, "read_env_value", return_value="service-key") as read_env_value, \
                patch.object(
                MODULE._LOOPBACK_OPENER,
                "open",
                return_value=FakeResponse(200, {"choices": [{"message": {"content": "synthetic-ok"}}]}),
            ) as opener:
            exit_code = MODULE.main([
                "--allow-real-api",
                "--env-file",
                ".env.canary",
                "--json",
            ])
        self.assertEqual(exit_code, 0)
        read_env_value.assert_called_once_with(Path(".env.canary"), "AURORA_CANARY_AUTHORIZATION")
        opener.assert_called_once()
        request = opener.call_args.args[0]
        self.assertEqual(opener.call_args.kwargs, {"timeout": 60})
        self.assertEqual(request.get_header("Authorization"), "Bearer service-key")
        rendered = output.getvalue()
        self.assertEqual(rendered.count("\n"), 1)
        self.assertEqual(json.loads(rendered)["classification"], "pass")
        self.assertNotIn("service-key", rendered)
        self.assertNotIn("synthetic-ok", rendered)

    def test_main_emits_the_same_single_json_line_without_the_compatibility_flag(self):
        output = io.StringIO()
        result = {
            "classification": "pass",
            "http_status": 200,
            "response_is_json": True,
            "choices_present": True,
            "message_present": True,
            "content_nonempty": True,
        }
        with redirect_stdout(output), \
                patch.object(MODULE, "read_env_value", return_value="service-key"), \
                patch.object(MODULE, "probe_once", return_value=result) as probe:
            exit_code = MODULE.main(["--allow-real-api"])
        self.assertEqual(exit_code, 0)
        probe.assert_called_once_with("service-key")
        self.assertEqual(output.getvalue(), MODULE.render_json(result) + "\n")

    def test_read_env_value_requires_one_nonempty_matching_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env.canary"
            env_file.write_text("AURORA_CANARY_AUTHORIZATION=service-key\n", encoding="utf-8")
            self.assertEqual(MODULE.read_env_value(env_file, "AURORA_CANARY_AUTHORIZATION"), "service-key")
            env_file.write_text("AURORA_CANARY_AUTHORIZATION=one\nAURORA_CANARY_AUTHORIZATION=two\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                MODULE.read_env_value(env_file, "AURORA_CANARY_AUTHORIZATION")

    def test_env_reader_rejects_every_noncanonical_authorization_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env.canary"
            for entry in INVALID_ENV_ENTRIES:
                with self.subTest(entry=repr(entry)):
                    env_file.write_text(entry, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        MODULE.read_env_value(env_file, "AURORA_CANARY_AUTHORIZATION")

    def test_invalid_env_format_cannot_make_a_request_or_become_auth_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env.canary"
            for entry in INVALID_ENV_ENTRIES:
                with self.subTest(entry=repr(entry)):
                    env_file.write_text(entry, encoding="utf-8")
                    output = io.StringIO()
                    with redirect_stdout(output), patch.object(MODULE._LOOPBACK_OPENER, "open") as opener:
                        exit_code = MODULE.main([
                            "--allow-real-api",
                            "--env-file",
                            str(env_file),
                            "--json",
                        ])
                    self.assertNotEqual(exit_code, 0)
                    opener.assert_not_called()
                    result = json.loads(output.getvalue())
                    self.assertEqual(result["classification"], "unavailable")
                    self.assertNotEqual(result["classification"], "auth_failed")
                    self.assertEqual(set(result), set(MODULE._RESULT_FIELDS))


if __name__ == "__main__":
    unittest.main()
