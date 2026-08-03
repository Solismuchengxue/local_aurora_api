import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "aurora_capability_canary.py"
SPEC = importlib.util.spec_from_file_location("aurora_capability_canary", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SafetyGateTests(unittest.TestCase):
    def test_only_fixed_loopback_canary_urls_are_accepted(self):
        self.assertEqual(
            MODULE.validate_canary_url("http://127.0.0.1:18080", "direct"),
            "http://127.0.0.1:18080",
        )
        self.assertEqual(
            MODULE.validate_canary_url("http://127.0.0.1:13000", "gateway"),
            "http://127.0.0.1:13000",
        )
        for url in (
            "http://127.0.0.1:8080",
            "http://127.0.0.1:3000",
            "http://192.0.2.10:18080",
            "https://127.0.0.1:18080",
            "https://chatgpt.com",
        ):
            with self.subTest(url=url), self.assertRaises(MODULE.ProbeError):
                MODULE.validate_canary_url(url, "direct")

    def test_missing_real_api_flag_exits_before_transport(self):
        with (
            mock.patch.object(MODULE, "run_matrix") as run,
            mock.patch.object(MODULE, "read_env_value") as read_env,
            mock.patch.object(MODULE, "read_single_secret") as read_secret,
            mock.patch.object(Path, "read_text") as read_file,
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                exit_code = MODULE.main([])
        self.assertEqual(exit_code, 2)
        run.assert_not_called()
        read_env.assert_not_called()
        read_secret.assert_not_called()
        read_file.assert_not_called()

    def test_secret_readers_require_one_nonempty_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env.canary"
            env.write_text("AURORA_CANARY_AUTHORIZATION=service-secret\n", encoding="utf-8")
            token = root / "token.txt"
            token.write_text("gateway-secret\n", encoding="utf-8")
            self.assertEqual(
                MODULE.read_env_value(env, "AURORA_CANARY_AUTHORIZATION"),
                "service-secret",
            )
            self.assertEqual(MODULE.read_single_secret(token), "gateway-secret")
            token.write_text("one\ntwo\n", encoding="utf-8")
            with self.assertRaises(MODULE.ProbeError):
                MODULE.read_single_secret(token)

    def test_env_reader_rejects_duplicate_blank_nul_and_unavailable_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env.canary"
            for contents in (
                "AURORA_CANARY_AUTHORIZATION=one\nAURORA_CANARY_AUTHORIZATION=two\n",
                "AURORA_CANARY_AUTHORIZATION= \t\n",
                "AURORA_CANARY_AUTHORIZATION=bad\x00value\n",
            ):
                env.write_text(contents, encoding="utf-8")
                with self.subTest(contents=repr(contents)), self.assertRaises(MODULE.ProbeError):
                    MODULE.read_env_value(env, "AURORA_CANARY_AUTHORIZATION")
            with self.assertRaises(MODULE.ProbeError):
                MODULE.read_env_value(root / "missing", "AURORA_CANARY_AUTHORIZATION")
            with mock.patch.object(Path, "read_text", side_effect=OSError):
                with self.assertRaises(MODULE.ProbeError):
                    MODULE.read_env_value(env, "AURORA_CANARY_AUTHORIZATION")


class SanitizedReportTests(unittest.TestCase):
    def test_report_keeps_only_allowlisted_boolean_count_and_media_details(self):
        report = MODULE.build_report(
            {
                "direct": [
                    MODULE.CheckResult("models", "PASS", "models_valid", {"count": 2}),
                    MODULE.CheckResult(
                        "image_generation",
                        "PASS",
                        "image_generation_valid",
                        {"bytes": 12, "media_type": "image/png", "decodable": True},
                    ),
                    MODULE.CheckResult("files", "FAIL", "route_missing", {}),
                ]
            },
            datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc),
        )
        parsed = json.loads(MODULE.serialize_report(report))
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["checked_at"], "2026-08-03T05:00:00Z")
        self.assertEqual(parsed["overall"], "FAIL")
        self.assertEqual(
            parsed["targets"]["direct"][1]["details"],
            {"bytes": 12, "media_type": "image/png", "decodable": True},
        )

    def test_report_rejects_unallowlisted_or_non_sanitized_details(self):
        with self.assertRaises(ValueError):
            MODULE.build_report(
                {
                    "direct": [
                        MODULE.CheckResult(
                            "chat_nonstream",
                            "PASS",
                            "chat_nonstream_valid",
                            {"content_present": "untrusted response"},
                        )
                    ]
                }
            )

    def test_serialization_revalidates_forged_report_input(self):
        forged_report = {
            "schema_version": 1,
            "checked_at": "2026-08-03T05:00:00Z",
            "overall": "PASS",
            "targets": {
                "direct": [
                    {
                        "name": "chat_nonstream",
                        "status": "PASS",
                        "code": "chat_nonstream_valid",
                        "details": {"content_present": "untrusted response"},
                    }
                ]
            },
        }
        with self.assertRaises(ValueError):
            MODULE.serialize_report(forged_report)
        with self.assertRaises(ValueError):
            MODULE.build_report(
                {
                    "direct": [
                        MODULE.CheckResult(
                            "chat_nonstream",
                            "FAIL",
                            "chat_empty",
                            {"body": "untrusted response"},
                        )
                    ]
                }
            )


class HttpTests(unittest.TestCase):
    def test_non_2xx_is_classified_without_response_body(self):
        cases = {
            401: "auth_failed",
            403: "upstream_forbidden",
            404: "route_missing",
            429: "rate_limited",
            502: "upstream_failed",
        }
        for status, code in cases.items():
            with self.subTest(status=status):
                response = MODULE.HttpResponse(status, {}, b"raw-private-upstream-body")
                with self.assertRaises(MODULE.ProbeError) as raised:
                    MODULE.require_success(response)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("private", str(raised.exception))

    def test_json_and_sse_are_bounded_and_strict(self):
        payload = MODULE.decode_json(MODULE.HttpResponse(200, {}, b'{"data":[]}'))
        self.assertEqual(payload, {"data": []})
        events = MODULE.parse_sse(
            b"event: response.created\ndata: {\"type\":\"response.created\"}\n\n"
            b"event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n"
            b"data: [DONE]\n\n"
        )
        self.assertEqual([event[0] for event in events], ["response.created", "response.completed", "done"])

    def test_malformed_json_and_sse_have_fixed_errors(self):
        for body, code in ((b"not-json", "json_invalid"), (b"[]", "json_invalid")):
            with self.subTest(body=body), self.assertRaises(MODULE.ProbeError) as raised:
                MODULE.decode_json(MODULE.HttpResponse(200, {}, body))
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.parse_sse(b"event: response.created\ndata: not-json\n\n")
        self.assertEqual(raised.exception.code, "sse_invalid")


class TextCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")

    @staticmethod
    def transport_for(response):
        def transport(*args, **kwargs):
            return response
        return transport

    def test_models_requires_expected_model_ids(self):
        response = MODULE.HttpResponse(
            200,
            {"content-type": "application/json"},
            b'{"data":[{"id":"gpt-5-6-pro"},{"id":"gpt-5-6-thinking"},{"id":"gpt-image-2"}]}'
        )
        result = MODULE.check_models(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code, result.details), ("PASS", "models_valid", {"count": 3}))

    def test_chat_nonstream_distinguishes_empty_content(self):
        response = MODULE.HttpResponse(
            200, {}, b'{"choices":[{"message":{"content":""}}]}'
        )
        result = MODULE.check_chat_nonstream(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code, result.details), ("FAIL", "chat_empty", {}))

    def test_chat_stream_requires_chunk_and_done(self):
        response = MODULE.HttpResponse(
            200,
            {"content-type": "text/event-stream"},
            b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\n'
            b'data: [DONE]\n\n',
        )
        result = MODULE.check_chat_stream(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code, result.details), ("PASS", "chat_stream_valid", {"chunks": 1, "done": True}))

    def test_responses_nonstream_requires_completed_output(self):
        response = MODULE.HttpResponse(
            200, {}, b'{"status":"completed","output":[{"type":"message"}]}'
        )
        result = MODULE.check_responses_nonstream(self.target, self.transport_for(response))
        self.assertEqual(
            (result.status, result.code, result.details),
            ("PASS", "responses_nonstream_valid", {"completed": True, "output_count": 1}),
        )

    def test_responses_stream_requires_completed_and_done(self):
        response = MODULE.HttpResponse(
            200,
            {"content-type": "text/event-stream"},
            b"event: response.created\ndata: {\"type\":\"response.created\"}\n\n"
            b"event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"synthetic\"}\n\n"
            b"event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n"
            b"data: [DONE]\n\n",
        )
        result = MODULE.check_responses_stream(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code), ("PASS", "responses_stream_valid"))
        self.assertEqual(result.details, {"created": True, "output_seen": True, "completed": True, "done": True})

    def test_malformed_chat_structure_returns_fixed_code_without_payload(self):
        response = MODULE.HttpResponse(200, {}, b'{"choices":[{"message":{"content":42}}]}')
        result = MODULE.check_chat_nonstream(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code, result.details), ("FAIL", "chat_nonstream_invalid", {}))
