import importlib.util
import base64
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import wave
from copy import deepcopy
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "new_api_multimodal_probe.py"


def load_module():
    if not MODULE_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "new_api_multimodal_probe", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


def target():
    return MODULE.TargetConfig(MODULE.NEW_API_BASE_URL, "sk-test-client")


def response(payload, *, content_type="application/json", status=200):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return MODULE.HttpResponse(
        status=status,
        headers={"content-type": content_type},
        body=body,
    )


def passing_report():
    details = {
        "models": {"required_present": True},
        "chat_nonstream": {"content_present": True},
        "chat_stream": {"chunks": 1, "done": True},
        "responses_nonstream": {"completed": True, "output_count": 1},
        "responses_stream": {
            "created": True,
            "output_seen": True,
            "completed": True,
            "done": True,
        },
        "files": {"uploaded": True, "referenced": True},
        "vision": {"image_uploaded": True, "image_understood": True},
        "image_generation": {
            "media_type": "image/png",
            "bytes": 68,
            "decodable": True,
        },
        "image_edit": {
            "media_type": "image/png",
            "bytes": 68,
            "decodable": True,
        },
        "image_variation": {
            "media_type": "image/png",
            "bytes": 68,
            "decodable": True,
        },
        "audio_speech": {
            "media_type": "audio/mpeg",
            "bytes": 128,
            "codec": "mp3",
            "sample_rate": 24000,
            "channels": 1,
        },
        "audio_transcription": {
            "text_present": True,
            "expected_marker_present": True,
        },
        "audio_translation": {
            "text_present": True,
            "english_markers_present": True,
        },
        "audio_translation_composed": {
            "text_present": True,
            "chinese_present": True,
        },
    }
    return {
        "schema_version": 1,
        "checked_at": "2026-08-04T12:00:00Z",
        "overall": "PASS",
        "checks": [
            {
                "name": name,
                "status": "PASS",
                "code": MODULE.SUCCESS_CODES[name],
                "details": details[name],
            }
            for name in MODULE.EXPECTED_CHECKS
        ],
    }


class SafetyGateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            MODULE,
            "scripts/new_api_multimodal_probe.py must be implemented",
        )
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "one-api.db"
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                CREATE TABLE tokens (
                    id INTEGER PRIMARY KEY,
                    key TEXT,
                    status INTEGER,
                    expired_time INTEGER,
                    unlimited_quota INTEGER,
                    remain_quota INTEGER
                )
                """
            )
            connection.executemany(
                "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, "expired", 1, 1, 1, 0),
                    (2, "sk-test-client", 1, -1, 0, 10),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def test_real_api_requires_explicit_flag(self):
        with mock.patch.object(MODULE, "run_matrix") as run:
            self.assertEqual(MODULE.main([]), 2)
        run.assert_not_called()

    def test_target_is_fixed_to_production_new_api(self):
        self.assertEqual(MODULE.NEW_API_BASE_URL, "http://127.0.0.1:3000")
        self.assertFalse(hasattr(MODULE.parse_args([]), "base_url"))

    def test_client_token_is_selected_without_being_reported(self):
        token = MODULE.read_client_token(self.database, now=1_786_000_000)
        self.assertEqual(token, "sk-test-client")
        report = MODULE.serialize_report(passing_report())
        self.assertNotIn("sk-test-client", report)


class SanitizedReportTests(unittest.TestCase):
    def test_report_requires_exact_check_order_and_overall(self):
        report = passing_report()
        report["checks"] = list(reversed(report["checks"]))
        with self.assertRaisesRegex(MODULE.ProbeError, "invalid_check_order"):
            MODULE.serialize_report(report)

        report = passing_report()
        report["overall"] = "FAIL"
        with self.assertRaisesRegex(MODULE.ProbeError, "invalid_report"):
            MODULE.serialize_report(report)

    def test_report_rejects_unknown_fields_and_sensitive_text(self):
        report = passing_report()
        report["checks"][0]["token"] = "sk-secret"
        with self.assertRaisesRegex(MODULE.ProbeError, "invalid_report"):
            MODULE.serialize_report(report)

        report = passing_report()
        report["checks"][0]["details"]["log"] = "upstream response body"
        with self.assertRaisesRegex(MODULE.ProbeError, "invalid_report"):
            MODULE.serialize_report(report)

    def test_report_rejects_wrong_media_type_and_unbounded_counts(self):
        report = passing_report()
        report["checks"][7]["details"]["media_type"] = "text/html"
        with self.assertRaisesRegex(MODULE.ProbeError, "invalid_report"):
            MODULE.serialize_report(report)

        report = passing_report()
        report["checks"][2]["details"]["chunks"] = 1_000_001
        with self.assertRaisesRegex(MODULE.ProbeError, "invalid_report"):
            MODULE.serialize_report(report)

    def test_serialization_is_deterministic_and_bounded(self):
        first = MODULE.serialize_report(passing_report())
        second = MODULE.serialize_report(deepcopy(passing_report()))
        self.assertEqual(first, second)
        self.assertLessEqual(len(first.encode("utf-8")), 32 * 1024)


class HttpAndTextCapabilityTests(unittest.TestCase):
    def setUp(self):
        required = (
            "TargetConfig",
            "HttpResponse",
            "http_request",
            "check_models",
            "check_chat_nonstream",
            "check_chat_stream",
            "check_responses_nonstream",
            "check_responses_stream",
        )
        missing = [name for name in required if not hasattr(MODULE, name)]
        self.assertEqual(missing, [], f"missing transport interfaces: {missing}")

    def test_transport_rejects_unapproved_paths_before_opening(self):
        with mock.patch.object(MODULE.urllib.request, "urlopen") as opened:
            with self.assertRaisesRegex(MODULE.ProbeError, "route"):
                MODULE.http_request(target(), "GET", "/api/admin/users")
        opened.assert_not_called()

    def test_transport_uses_fixed_loopback_and_bounded_read(self):
        upstream = mock.MagicMock()
        upstream.status = 200
        upstream.headers = {"Content-Type": "application/json"}
        upstream.read.side_effect = [b'{"data":[]}', b""]
        context = mock.MagicMock()
        context.__enter__.return_value = upstream
        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=context) as opened:
            result = MODULE.http_request(target(), "GET", "/v1/models")

        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:3000/v1/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(result.status, 200)
        self.assertEqual(upstream.read.call_args_list[0].args[0], MODULE.MAX_RESPONSE_BYTES + 1)

    def test_models_and_nonstream_chat_require_structured_results(self):
        calls = []

        def transport(_target, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/v1/models":
                return response(
                    {
                        "data": [
                            {"id": "gpt-4o"},
                            {"id": "gpt-image-2"},
                            {"id": "tts-1"},
                            {"id": "whisper-1"},
                        ]
                    }
                )
            return response(
                {"choices": [{"message": {"content": "structured"}}]}
            )

        models = MODULE.check_models(target(), transport)
        chat = MODULE.check_chat_nonstream(target(), transport)
        self.assertEqual((models.status, models.code), ("PASS", "models_valid"))
        self.assertEqual(
            (chat.status, chat.code), ("PASS", "chat_nonstream_valid")
        )
        self.assertEqual([item[1] for item in calls], ["/v1/models", "/v1/chat/completions"])
        chat_payload = json.loads(calls[1][2]["body"])
        self.assertEqual(chat_payload["model"], "gpt-4o")
        self.assertFalse(chat_payload["stream"])

    def test_streaming_checks_require_sse_completion(self):
        chat_sse = (
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        responses_sse = (
            b'event: response.created\ndata: {"type":"response.created"}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
            b"data: [DONE]\n\n"
        )

        def transport(_target, _method, path, **_kwargs):
            body = chat_sse if path == "/v1/chat/completions" else responses_sse
            return response(body, content_type="text/event-stream; charset=utf-8")

        chat = MODULE.check_chat_stream(target(), transport)
        responses = MODULE.check_responses_stream(target(), transport)
        self.assertEqual((chat.status, chat.code), ("PASS", "chat_stream_valid"))
        self.assertEqual(
            (responses.status, responses.code),
            ("PASS", "responses_stream_valid"),
        )

    def test_responses_nonstream_requires_completed_output(self):
        passed = MODULE.check_responses_nonstream(
            target(),
            lambda *_args, **_kwargs: response(
                {"status": "completed", "output": [{"type": "message"}]}
            ),
        )
        failed = MODULE.check_responses_nonstream(
            target(),
            lambda *_args, **_kwargs: response(
                {"status": "completed", "output": []}
            ),
        )
        self.assertEqual((passed.status, passed.code), ("PASS", "responses_nonstream_valid"))
        self.assertEqual((failed.status, failed.code), ("FAIL", "relay"))

    def test_http_failure_is_classified_without_body_or_exception_text(self):
        result = MODULE.check_chat_nonstream(
            target(),
            lambda *_args, **_kwargs: response(
                b'{"error":{"message":"secret upstream body"}}', status=401
            ),
        )
        self.assertEqual((result.status, result.code, result.details), ("FAIL", "auth", {}))


class FileAndImageCapabilityTests(unittest.TestCase):
    def setUp(self):
        required = (
            "make_test_png",
            "encode_multipart",
            "decode_image_result",
            "check_files",
            "check_vision",
            "check_image_generation",
            "check_image_edit",
            "check_image_variation",
        )
        missing = [name for name in required if not hasattr(MODULE, name)]
        self.assertEqual(missing, [], f"missing image interfaces: {missing}")

    def test_png_and_multipart_are_bounded_and_deterministic(self):
        blue = MODULE.make_test_png((0, 0, 255))
        self.assertEqual(blue, MODULE.make_test_png((0, 0, 255)))
        self.assertTrue(blue.startswith(b"\x89PNG\r\n\x1a\n"))
        content_type, body = MODULE.encode_multipart(
            fields={"model": "gpt-image-2"},
            files={"image": ("fixture.png", "image/png", blue)},
        )
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="model"', body)
        self.assertIn(b'name="image"; filename="fixture.png"', body)
        self.assertNotIn(str(ROOT).encode(), body)
        self.assertLessEqual(len(body), MODULE.MAX_REQUEST_BYTES)

    def test_image_result_requires_decodable_inline_png(self):
        blue = MODULE.make_test_png((0, 0, 255))
        payload = {
            "data": [{"b64_json": base64.b64encode(blue).decode("ascii")}]
        }
        details, decoded = MODULE.decode_image_result(payload)
        self.assertEqual(decoded, blue)
        self.assertEqual(
            details,
            {"media_type": "image/png", "bytes": len(blue), "decodable": True},
        )
        with self.assertRaisesRegex(MODULE.ProbeError, "invalid_media"):
            MODULE.decode_image_result({"data": [{"url": "https://example.invalid/x"}]})

    def test_generation_edit_and_variation_use_expected_routes(self):
        source = MODULE.make_test_png((0, 0, 255))
        changed = MODULE.make_test_png((255, 0, 0))
        calls = []

        def transport(_target, method, path, **kwargs):
            calls.append((method, path, kwargs))
            payload = {
                "data": [
                    {"b64_json": base64.b64encode(changed).decode("ascii")}
                ]
            }
            return response(payload)

        results = [
            MODULE.check_image_generation(target(), transport),
            MODULE.check_image_edit(target(), transport),
            MODULE.check_image_variation(target(), transport),
        ]
        self.assertEqual([item.status for item in results], ["PASS"] * 3)
        self.assertEqual(
            [item[1] for item in calls],
            ["/v1/images/generations", "/v1/images/edits", "/v1/images/variations"],
        )
        self.assertEqual(calls[0][2]["content_type"], "application/json")
        self.assertTrue(calls[1][2]["content_type"].startswith("multipart/form-data"))
        self.assertIn(source, calls[1][2]["body"])

    def test_vision_requires_observed_color_not_prompt_echo(self):
        calls = []

        def passing(_target, _method, path, **kwargs):
            calls.append((path, json.loads(kwargs["body"])))
            return response({"choices": [{"message": {"content": "BLUE"}}]})

        passed = MODULE.check_vision(target(), passing)
        failed = MODULE.check_vision(
            target(),
            lambda *_args, **_kwargs: response(
                {"choices": [{"message": {"content": "RED"}}]}
            ),
        )
        self.assertEqual((passed.status, passed.code), ("PASS", "vision_valid"))
        self.assertEqual((failed.status, failed.code), ("FAIL", "semantic_mismatch"))
        content = calls[0][1]["messages"][0]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_files_upload_then_reference_uses_each_route_once(self):
        calls = []

        def transport(_target, _method, path, **kwargs):
            calls.append(path)
            if path == "/v1/files":
                return response({"id": "file-test"})
            return response(
                {"choices": [{"message": {"content": "NEW-API-FILE-OK"}}]}
            )

        result = MODULE.check_files(target(), transport)
        self.assertEqual((result.status, result.code), ("PASS", "files_valid"))
        self.assertEqual(calls, ["/v1/files", "/v1/chat/completions"])


class AudioCapabilityTests(unittest.TestCase):
    def setUp(self):
        required = (
            "make_english_wav",
            "validate_audio",
            "check_audio_speech",
            "check_audio_transcription",
            "check_audio_translation",
            "check_audio_translation_composed",
        )
        missing = [name for name in required if not hasattr(MODULE, name)]
        self.assertEqual(missing, [], f"missing audio interfaces: {missing}")

    def test_embedded_english_wav_is_bounded_pcm(self):
        audio = MODULE.make_english_wav()
        self.assertEqual(audio, MODULE.make_english_wav())
        self.assertLessEqual(len(audio), MODULE.MAX_REQUEST_BYTES)
        with wave.open(io.BytesIO(audio), "rb") as source:
            self.assertEqual(source.getnchannels(), 1)
            self.assertEqual(source.getsampwidth(), 2)
            self.assertIn(source.getframerate(), (16000, 22050))
            self.assertGreater(source.getnframes(), source.getframerate() // 2)

    def test_mp3_validation_uses_ffprobe_stdin_and_reports_metadata_only(self):
        completed = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "format": {"duration": "1.25"},
                    "streams": [
                        {
                            "codec_type": "audio",
                            "codec_name": "mp3",
                            "sample_rate": "24000",
                            "channels": 1,
                        }
                    ],
                }
            ).encode(),
        )
        with mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/ffprobe"), mock.patch.object(
            MODULE.subprocess, "run", return_value=completed
        ) as run:
            details = MODULE.validate_audio(b"ID3synthetic", "audio/mpeg")
        self.assertEqual(
            details,
            {
                "media_type": "audio/mpeg",
                "bytes": 12,
                "codec": "mp3",
                "sample_rate": 24000,
                "channels": 1,
            },
        )
        self.assertEqual(run.call_args.kwargs["input"], b"ID3synthetic")
        self.assertNotIn("ID3synthetic", json.dumps(details))

    def test_tts_requests_mp3_and_returns_sanitized_details(self):
        completed = mock.Mock(
            returncode=0,
            stdout=b'{"format":{"duration":"1.0"},"streams":[{"codec_type":"audio","codec_name":"mp3","sample_rate":"24000","channels":1}]}',
        )
        calls = []

        def transport(_target, _method, path, **kwargs):
            calls.append((path, json.loads(kwargs["body"])))
            return response(b"ID3synthetic", content_type="audio/mpeg")

        with mock.patch.object(MODULE.shutil, "which", return_value="ffprobe"), mock.patch.object(
            MODULE.subprocess, "run", return_value=completed
        ):
            result = MODULE.check_audio_speech(target(), transport)
        self.assertEqual((result.status, result.code), ("PASS", "audio_speech_valid"))
        self.assertEqual(calls[0][0], "/v1/audio/speech")
        self.assertEqual(calls[0][1]["response_format"], "mp3")

    def test_transcription_native_translation_and_composed_translation(self):
        audio = b"RIFFsynthetic"
        paths = []

        def transport(_target, _method, path, **_kwargs):
            paths.append(path)
            if path == "/v1/audio/transcriptions":
                return response({"text": "Today capability test"})
            if path == "/v1/audio/translations":
                return response({"text": "Today capability test"})
            return response(
                {"choices": [{"message": {"content": "今天进行能力测试"}}]}
            )

        transcription, text = MODULE.check_audio_transcription(
            target(), audio, transport
        )
        translation = MODULE.check_audio_translation(target(), audio, transport)
        composed = MODULE.check_audio_translation_composed(
            target(), text, transport
        )
        self.assertEqual(transcription.status, "PASS")
        self.assertEqual(translation.status, "PASS")
        self.assertEqual(composed.status, "PASS")
        self.assertEqual(
            paths,
            [
                "/v1/audio/transcriptions",
                "/v1/audio/translations",
                "/v1/chat/completions",
            ],
        )
        serialized = json.dumps(
            [transcription.details, translation.details, composed.details]
        )
        self.assertNotIn("Today capability test", serialized)
        self.assertNotIn("今天进行能力测试", serialized)

    def test_native_translation_rejects_chinese_or_missing_markers(self):
        result = MODULE.check_audio_translation(
            target(),
            b"RIFFsynthetic",
            lambda *_args, **_kwargs: response({"text": "今天进行能力测试"}),
        )
        self.assertEqual(
            (result.status, result.code, result.details),
            ("FAIL", "semantic_mismatch", {}),
        )


class OrchestrationAndCliTests(unittest.TestCase):
    def setUp(self):
        required = ("build_report", "atomic_write", "run_matrix")
        missing = [name for name in required if not hasattr(MODULE, name)]
        self.assertEqual(missing, [], f"missing orchestration interfaces: {missing}")

    def check_result(self, name):
        item = next(
            item for item in passing_report()["checks"] if item["name"] == name
        )
        return MODULE.CheckResult(**item)

    def test_run_matrix_calls_each_check_once_in_fixed_order(self):
        patches = {}
        for name in MODULE.EXPECTED_CHECKS:
            function_name = f"check_{name}"
            result = self.check_result(name)
            return_value = (
                (result, "Today capability test")
                if name == "audio_transcription"
                else result
            )
            patches[function_name] = mock.patch.object(
                MODULE, function_name, return_value=return_value
            )
        started = [patcher.start() for patcher in patches.values()]
        self.addCleanup(mock.patch.stopall)
        with mock.patch.object(MODULE, "make_english_wav", return_value=b"RIFFfixture"):
            results = MODULE.run_matrix(target(), transport=mock.sentinel.transport)
        self.assertEqual(tuple(item.name for item in results), MODULE.EXPECTED_CHECKS)
        self.assertTrue(all(mocked.call_count == 1 for mocked in started))

    def test_build_report_and_atomic_write_are_deterministic(self):
        results = [self.check_result(name) for name in MODULE.EXPECTED_CHECKS]
        report = MODULE.build_report(results, checked_at="2026-08-04T12:00:00Z")
        self.assertEqual(report, passing_report())
        payload = MODULE.serialize_report(report).encode()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.json"
            MODULE.atomic_write(output, payload)
            self.assertEqual(output.read_bytes(), payload)
            MODULE.atomic_write(output, payload)
            self.assertEqual(list(Path(temporary).iterdir()), [output])

    def test_cli_reads_token_in_memory_and_redacts_stdout(self):
        results = [self.check_result(name) for name in MODULE.EXPECTED_CHECKS]
        stdout = io.StringIO()
        with mock.patch.object(MODULE, "read_client_token", return_value="sk-secret"), mock.patch.object(
            MODULE, "run_matrix", return_value=results
        ) as run, contextlib.redirect_stdout(stdout):
            exit_code = MODULE.main(
                [
                    "--allow-real-api",
                    "--root",
                    str(MODULE.EXPECTED_ROOT),
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertNotIn("sk-secret", stdout.getvalue())
        self.assertEqual(run.call_args.args[0].base_url, MODULE.NEW_API_BASE_URL)
        self.assertEqual(run.call_args.args[0].key, "sk-secret")


class SchemaContractTests(unittest.TestCase):
    def test_schema_locks_top_level_and_exact_check_order(self):
        path = ROOT / "docs" / "contracts" / "new-api-multimodal-report-v1.schema.json"
        self.assertTrue(path.is_file(), "report schema must be implemented")
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["required"],
            ["schema_version", "checked_at", "overall", "checks"],
        )
        self.assertFalse(schema["additionalProperties"])
        checks = schema["properties"]["checks"]
        self.assertEqual(checks["minItems"], 14)
        self.assertEqual(checks["maxItems"], 14)
        names = [
            item["properties"]["name"]["const"]
            for item in checks["prefixItems"]
        ]
        self.assertEqual(tuple(names), MODULE.EXPECTED_CHECKS)
        for item in checks["prefixItems"]:
            self.assertEqual(
                item["required"], ["name", "status", "code", "details"]
            )
            self.assertFalse(item["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
