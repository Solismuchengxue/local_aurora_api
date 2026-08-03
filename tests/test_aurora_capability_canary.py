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
