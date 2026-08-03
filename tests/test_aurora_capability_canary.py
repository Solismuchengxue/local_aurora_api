import contextlib
import base64
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import wave


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "aurora_capability_canary.py"
SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "contracts"
    / "aurora-capability-canary-report-v1.schema.json"
)
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
        results = passing_results()
        results[5] = MODULE.CheckResult("files", "FAIL", "route_missing", {})
        results[6] = MODULE.CheckResult(
            "image_generation",
            "PASS",
            "image_generation_valid",
            {"bytes": 12, "media_type": "image/png", "decodable": True},
        )
        report = MODULE.build_report(
            {"direct": results},
            datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc),
        )
        parsed = json.loads(MODULE.serialize_report(report))
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["checked_at"], "2026-08-03T05:00:00Z")
        self.assertEqual(parsed["overall"], "FAIL")
        self.assertEqual(
            parsed["targets"]["direct"][6]["details"],
            {"bytes": 12, "media_type": "image/png", "decodable": True},
        )

    def test_report_rejects_sparse_out_of_order_and_duplicate_target_results(self):
        sparse = passing_results()[:1]
        out_of_order = passing_results()
        out_of_order[0], out_of_order[1] = out_of_order[1], out_of_order[0]
        duplicate = passing_results()
        duplicate[1] = duplicate[0]
        for results in (sparse, out_of_order, duplicate):
            with self.subTest(results=[result.name for result in results]):
                with self.assertRaises(ValueError):
                    MODULE.build_report({"direct": results})

    def test_report_rejects_name_status_code_and_detail_mismatches(self):
        invalid_results = []
        wrong_name_code = passing_results()
        wrong_name_code[0] = MODULE.CheckResult(
            "models", "PASS", "chat_nonstream_valid", {"content_present": True}
        )
        invalid_results.append(wrong_name_code)
        wrong_status_code = passing_results()
        wrong_status_code[0] = MODULE.CheckResult("models", "FAIL", "models_valid", {})
        invalid_results.append(wrong_status_code)
        wrong_details = passing_results()
        wrong_details[0] = MODULE.CheckResult(
            "models", "PASS", "models_valid", {"content_present": True}
        )
        invalid_results.append(wrong_details)
        for results in invalid_results:
            with self.subTest(result=results[0]):
                with self.assertRaises(ValueError):
                    MODULE.build_report({"direct": results})

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
    class RecordingResponse:
        def __init__(self, payload):
            self.status = 200
            self.headers = {"X-Trace": "synthetic"}
            self.payload = payload
            self.read_sizes = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            self.read_sizes.append(size)
            return self.payload

    def test_http_request_is_bounded_sanitized_and_uses_fixed_request_shape(self):
        response = self.RecordingResponse(b'{"data":[]}')
        target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")
        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response) as urlopen:
            result = MODULE.http_request(target, "GET", "/v1/models")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:18080/v1/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(response.read_sizes, [MODULE.MAX_RESPONSE_BYTES + 1])
        self.assertEqual(result, MODULE.HttpResponse(200, {"x-trace": "synthetic"}, b'{"data":[]}'))
        self.assertNotIn("authorization", result.headers)
        self.assertNotIn("secret", repr(result))

    def test_http_request_rejects_unsafe_method_and_path_before_urlopen(self):
        target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")
        with mock.patch.object(MODULE.urllib.request, "urlopen") as urlopen:
            for method, path in (("DELETE", "/v1/models"), ("GET", "/v1/not-allowed")):
                with self.subTest(method=method, path=path), self.assertRaises(MODULE.ProbeError) as raised:
                    MODULE.http_request(target, method, path)
                self.assertEqual(raised.exception.code, "unsafe_request")
        urlopen.assert_not_called()

    def test_http_request_rejects_oversized_response_and_does_not_read_http_error_body(self):
        target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")
        oversized = self.RecordingResponse(b"x" * (MODULE.MAX_RESPONSE_BYTES + 1))
        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=oversized):
            with self.assertRaises(MODULE.ProbeError) as raised:
                MODULE.http_request(target, "GET", "/v1/models")
        self.assertEqual(raised.exception.code, "response_too_large")
        error = MODULE.urllib.error.HTTPError("http://example.invalid", 401, "unauthorized", {}, None)
        error.read = mock.Mock(side_effect=AssertionError("body must stay unread"))
        with mock.patch.object(MODULE.urllib.request, "urlopen", side_effect=error):
            result = MODULE.http_request(target, "GET", "/v1/models")
        self.assertEqual(result, MODULE.HttpResponse(401, {}, b""))
        error.read.assert_not_called()

    def test_http_request_maps_local_transport_exceptions_to_fixed_codes(self):
        target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")
        cases = (
            (TimeoutError("synthetic timeout"), "timeout"),
            (OSError("synthetic socket failure"), "connectivity_failed"),
            (MODULE.urllib.error.URLError("synthetic url failure"), "connectivity_failed"),
            (MODULE.http.client.HTTPException("synthetic protocol failure"), "connectivity_failed"),
        )
        for error, code in cases:
            with self.subTest(error=type(error).__name__), mock.patch.object(
                MODULE.urllib.request, "urlopen", side_effect=error
            ) as urlopen, self.assertRaises(MODULE.ProbeError) as raised:
                MODULE.http_request(target, "GET", "/v1/models")
            self.assertEqual(raised.exception.code, code)
            urlopen.assert_called_once()

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
        for body, code in (
            (b"not-json", "json_invalid"),
            (b"[]", "json_invalid"),
            (b"\xff", "json_invalid"),
            (b"x" * (MODULE.MAX_JSON_BYTES + 1), "json_too_large"),
        ):
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

    def test_models_missing_required_id_has_fixed_code(self):
        response = MODULE.HttpResponse(200, {}, b'{"data":[{"id":"gpt-5-6-pro"}]}')
        result = MODULE.check_models(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code, result.details), ("FAIL", "models_invalid", {}))

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

    def test_chat_stream_rejects_unrelated_json_before_done(self):
        response = MODULE.HttpResponse(200, {}, b'data: {"unrelated":true}\n\ndata: [DONE]\n\n')
        result = MODULE.check_chat_stream(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code, result.details), ("FAIL", "chat_stream_invalid", {}))

    def test_responses_nonstream_requires_completed_output(self):
        response = MODULE.HttpResponse(
            200, {}, b'{"status":"completed","output":[{"type":"message"}]}'
        )
        result = MODULE.check_responses_nonstream(self.target, self.transport_for(response))
        self.assertEqual(
            (result.status, result.code, result.details),
            ("PASS", "responses_nonstream_valid", {"completed": True, "output_count": 1}),
        )

    def test_responses_nonstream_rejects_missing_output(self):
        response = MODULE.HttpResponse(200, {}, b'{"status":"completed","output":[]}')
        result = MODULE.check_responses_nonstream(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code, result.details), ("FAIL", "responses_nonstream_invalid", {}))

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

    def test_responses_stream_rejects_event_type_mismatch(self):
        response = MODULE.HttpResponse(
            200,
            {},
            b'event: response.created\ndata: {"type":"unrelated"}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"unrelated"}\n\n'
            b'event: response.completed\ndata: {"type":"unrelated"}\n\n'
            b'data: [DONE]\n\n',
        )
        result = MODULE.check_responses_stream(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code, result.details), ("FAIL", "responses_stream_invalid", {}))

    def test_malformed_chat_structure_returns_fixed_code_without_payload(self):
        response = MODULE.HttpResponse(200, {}, b'{"choices":[{"message":{"content":42}}]}')
        result = MODULE.check_chat_nonstream(self.target, self.transport_for(response))
        self.assertEqual((result.status, result.code, result.details), ("FAIL", "chat_nonstream_invalid", {}))


class MultipartAndImageHelperTests(unittest.TestCase):
    def test_test_png_is_deterministic_and_valid(self):
        first = MODULE.make_test_png()
        second = MODULE.make_test_png()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(first), 16 * 1024)

    def test_multipart_contains_names_but_not_local_paths(self):
        content_type, body = MODULE.encode_multipart(
            fields={"model": "gpt-image-2", "prompt": "synthetic edit"},
            files={"image": ("source.png", "image/png", MODULE.make_test_png())},
            boundary="aurora-canary-boundary",
        )
        self.assertEqual(content_type, "multipart/form-data; boundary=aurora-canary-boundary")
        self.assertIn(b'filename="source.png"', body)
        self.assertNotIn(str(Path.cwd()).encode(), body)

    def test_multipart_rejects_over_eight_mib_requests(self):
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.encode_multipart(
                fields={},
                files={"file": ("canary.bin", "application/octet-stream", b"x" * (8 * 1024 * 1024))},
                boundary="aurora-canary-boundary",
            )
        self.assertEqual(raised.exception.code, "request_too_large")

    def test_multipart_rejects_unsafe_header_fragments_without_echoing_them(self):
        for fields, files, boundary in (
            ({"field\r\nX-Injected: 1": "value"}, {}, "aurora-canary-boundary"),
            ({'field"name': "value"}, {}, "aurora-canary-boundary"),
            ({"field\x1fname": "value"}, {}, "aurora-canary-boundary"),
            ({}, {"image": ("source.png", "image/png\r\nX-Injected: 1", b"x")}, "aurora-canary-boundary"),
            ({}, {"image": ("source.png", 'image/"png', b"x")}, "aurora-canary-boundary"),
            ({}, {"image": ("source.png", "image/png; charset=utf-8", b"x")}, "aurora-canary-boundary"),
            ({}, {"image": ("source.png", "image/\x1fpng", b"x")}, "aurora-canary-boundary"),
            ({}, {}, "aurora\r\nX-Injected: 1"),
            ({}, {}, 'aurora"boundary'),
            ({}, {}, "aurora;boundary"),
            ({}, {}, "aurora\x1fboundary"),
        ):
            with self.subTest(fields=bool(fields), files=bool(files), boundary=repr(boundary)):
                with self.assertRaises(MODULE.ProbeError) as raised:
                    MODULE.encode_multipart(fields=fields, files=files, boundary=boundary)
                self.assertEqual((raised.exception.code, str(raised.exception)), ("multipart_invalid", "multipart_invalid"))


def image_json(image: bytes) -> bytes:
    return json.dumps({"data": [{"b64_json": base64.b64encode(image).decode("ascii")}]}).encode("utf-8")


class ImageCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")

    def test_generation_accepts_decodable_b64_only(self):
        result = MODULE.check_image_generation(
            self.target,
            lambda *args, **kwargs: MODULE.HttpResponse(200, {}, image_json(MODULE.make_test_png())),
        )
        self.assertEqual((result.status, result.code), ("PASS", "image_generation_valid"))
        self.assertEqual(result.details["media_type"], "image/png")
        self.assertNotIn("b64_json", result.details)

    def test_image_results_reject_malformed_base64_and_url_only(self):
        malformed = MODULE.check_image_generation(
            self.target,
            lambda *args, **kwargs: MODULE.HttpResponse(200, {}, b'{"data":[{"b64_json":"%%%"}]}'),
        )
        url_only = MODULE.check_image_generation(
            self.target,
            lambda *args, **kwargs: MODULE.HttpResponse(200, {}, b'{"data":[{"url":"https://example.invalid/private"}]}'),
        )
        mixed_url = MODULE.check_image_generation(
            self.target,
            lambda *args, **kwargs: MODULE.HttpResponse(
                200,
                {},
                json.dumps({"data": [{"url": "https://example.invalid/private", "b64_json": base64.b64encode(MODULE.make_test_png()).decode("ascii")}]}).encode("utf-8"),
            ),
        )
        self.assertEqual((malformed.status, malformed.code, malformed.details), ("FAIL", "image_payload_invalid", {}))
        self.assertEqual((url_only.status, url_only.code, url_only.details), ("FAIL", "image_url_not_accepted", {}))
        self.assertEqual((mixed_url.status, mixed_url.code, mixed_url.details), ("FAIL", "image_url_not_accepted", {}))

    def test_image_results_accept_minimal_png_jpeg_and_webp_signatures(self):
        for image, media_type in (
            (MODULE.make_test_png(), "image/png"),
            (b"\xff\xd8\xff\x00", "image/jpeg"),
            (b"RIFF\x04\x00\x00\x00WEBP", "image/webp"),
        ):
            with self.subTest(media_type=media_type):
                result = MODULE.check_image_generation(
                    self.target,
                    lambda *args, **kwargs: MODULE.HttpResponse(200, {}, image_json(image)),
                )
                self.assertEqual((result.status, result.code), ("PASS", "image_generation_valid"))
                self.assertEqual(result.details, {"bytes": len(image), "media_type": media_type, "decodable": True})

    def test_image_requests_use_the_expected_json_and_multipart_contracts(self):
        calls = []

        def transport(*args, **kwargs):
            calls.append((args, kwargs))
            return MODULE.HttpResponse(200, {}, image_json(MODULE.make_test_png()))

        generation = MODULE.check_image_generation(self.target, transport)
        edit = MODULE.check_image_edit(self.target, transport)
        variation = MODULE.check_image_variation(self.target, transport)
        self.assertEqual([generation.code, edit.code, variation.code], ["image_generation_valid", "image_edit_valid", "image_variation_valid"])
        self.assertEqual(calls[0][0][2], "/v1/images/generations")
        self.assertEqual(json.loads(calls[0][1]["body"]), {"model": "gpt-image-2", "prompt": MODULE.IMAGE_GENERATION_PROMPT, "n": 1, "size": "1024x1024", "response_format": "b64_json"})
        self.assertEqual(calls[1][0][2], "/v1/images/edits")
        self.assertEqual(calls[2][0][2], "/v1/images/variations")
        self.assertTrue(calls[1][1]["content_type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'filename="aurora-canary.png"', calls[1][1]["body"])
        self.assertIn(b'name="prompt"', calls[1][1]["body"])
        self.assertNotIn(b'name="prompt"', calls[2][1]["body"])


class FileCapabilityTests(unittest.TestCase):
    def test_files_upload_then_chat_returns_sanitized_booleans(self):
        target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")
        calls = []

        def transport(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                return MODULE.HttpResponse(200, {}, b'{"id":"file-synthetic-private-id"}')
            return MODULE.HttpResponse(200, {}, b'{"choices":[{"message":{"content":"AURORA-CANARY-FILE-OK"}}]}')

        result = MODULE.check_files(target, transport)
        self.assertEqual((result.status, result.code, result.details), ("PASS", "files_valid", {"upload_accepted": True, "file_id_present": True, "answer_present": True}))
        self.assertEqual(calls[0][0][2], "/v1/files")
        self.assertIn(b'filename="aurora-canary.txt"', calls[0][1]["body"])
        self.assertIn(b"AURORA CANARY SYNTHETIC FILE", calls[0][1]["body"])
        self.assertIn(b'name="purpose"', calls[0][1]["body"])
        self.assertEqual(calls[1][0][2], "/v1/chat/completions")
        self.assertIn("file-synthetic-private-id", calls[1][1]["body"].decode("utf-8"))
        self.assertNotIn("file-synthetic-private-id", repr(result))


def make_wav() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 1600)
    return stream.getvalue()


class AudioHelperTests(unittest.TestCase):
    def test_wav_must_be_decodable_and_bounded(self):
        audio = make_wav()
        details = MODULE.validate_audio(audio, "audio/wav")
        self.assertEqual(details, {"bytes": len(audio), "media_type": "audio/wav", "decodable": True})
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.validate_audio(b"not-audio", "audio/wav")
        self.assertEqual(raised.exception.code, "audio_payload_invalid")

    def test_non_wav_formats_require_matching_bounded_signatures(self):
        cases = (
            (b"\xff\xfb\x90\x00", "audio/mpeg"),
            (b"OggS\x00\x02", "audio/ogg"),
            (b"OggS\x00\x02OpusHead", "audio/opus"),
            (b"fLaC\x00\x00\x00\x22", "audio/flac"),
            (b"\xff\xf1\x50\x80", "audio/aac"),
            (b"\x1aE\xdf\xa3\x93B\x82\x88webm", "audio/webm"),
        )
        for audio, media_type in cases:
            with self.subTest(media_type=media_type):
                self.assertEqual(MODULE.validate_audio(audio, media_type), {"bytes": len(audio), "media_type": media_type, "decodable": True})
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.validate_audio(b"OggS\x00\x02", "audio/mpeg")
        self.assertEqual(raised.exception.code, "audio_payload_invalid")


class AudioCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.target = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "secret")

    def test_audio_chain_uses_in_memory_wav_and_sanitized_results(self):
        audio = make_wav()
        calls = []

        def transport(*args, **kwargs):
            calls.append((args, kwargs))
            path = args[2]
            if path == "/v1/audio/speech":
                return MODULE.HttpResponse(200, {"content-type": "audio/wav"}, audio)
            if path == "/v1/audio/transcriptions":
                return MODULE.HttpResponse(200, {}, '{"text":"这是一次能力测试。"}'.encode("utf-8"))
            if path == "/v1/audio/translations":
                return MODULE.HttpResponse(200, {}, b'{"text":"This is a capability test."}')
            self.fail(f"unexpected path: {path}")

        speech, returned_audio = MODULE.check_audio_speech(self.target, transport)
        transcription = MODULE.check_audio_transcription(self.target, returned_audio, transport)
        translation = MODULE.check_audio_translation(self.target, returned_audio, transport)
        self.assertEqual((speech.status, speech.code, speech.details), ("PASS", "audio_speech_valid", {"bytes": len(audio), "media_type": "audio/wav", "decodable": True}))
        self.assertEqual((transcription.status, transcription.code, transcription.details), ("PASS", "audio_transcription_valid", {"text_present": True, "expected_marker_present": True}))
        self.assertEqual((translation.status, translation.code, translation.details), ("PASS", "audio_translation_valid", {"text_present": True, "english_markers_present": True}))
        self.assertEqual([call[0][2] for call in calls], ["/v1/audio/speech", "/v1/audio/transcriptions", "/v1/audio/translations"])
        self.assertEqual(json.loads(calls[0][1]["body"]), {"model": "tts-1", "input": "今天是能力测试。", "voice": "alloy", "response_format": "wav"})
        self.assertIn(b'filename="aurora-canary.wav"', calls[1][1]["body"])
        self.assertIn(b'name="language"', calls[1][1]["body"])
        self.assertIn(b'filename="aurora-canary.wav"', calls[2][1]["body"])
        self.assertNotIn("能力测试", repr(transcription))
        self.assertNotIn("capability test", repr(translation))

    def test_audio_text_mismatches_return_fixed_codes_without_text(self):
        audio = make_wav()
        transcription = MODULE.check_audio_transcription(
            self.target,
            audio,
            lambda *args, **kwargs: MODULE.HttpResponse(200, {}, b'{"text":"not the marker"}'),
        )
        translation = MODULE.check_audio_translation(
            self.target,
            audio,
            lambda *args, **kwargs: MODULE.HttpResponse(200, {}, b'{"text":"not the marker"}'),
        )
        self.assertEqual((transcription.status, transcription.code, transcription.details), ("FAIL", "transcription_mismatch", {}))
        self.assertEqual((translation.status, translation.code, translation.details), ("FAIL", "translation_mismatch", {}))


def passing_results():
    return [
        MODULE.CheckResult("models", "PASS", "models_valid", {"count": 3}),
        MODULE.CheckResult("chat_nonstream", "PASS", "chat_nonstream_valid", {"content_present": True}),
        MODULE.CheckResult("chat_stream", "PASS", "chat_stream_valid", {"chunks": 1, "done": True}),
        MODULE.CheckResult("responses_nonstream", "PASS", "responses_nonstream_valid", {"completed": True, "output_count": 1}),
        MODULE.CheckResult("responses_stream", "PASS", "responses_stream_valid", {"created": True, "output_seen": True, "completed": True, "done": True}),
        MODULE.CheckResult("files", "PASS", "files_valid", {"upload_accepted": True, "file_id_present": True, "answer_present": True}),
        MODULE.CheckResult("image_generation", "PASS", "image_generation_valid", {"bytes": 1, "media_type": "image/png", "decodable": True}),
        MODULE.CheckResult("image_edit", "PASS", "image_edit_valid", {"bytes": 1, "media_type": "image/png", "decodable": True}),
        MODULE.CheckResult("image_variation", "PASS", "image_variation_valid", {"bytes": 1, "media_type": "image/png", "decodable": True}),
        MODULE.CheckResult("audio_speech", "PASS", "audio_speech_valid", {"bytes": 1, "media_type": "audio/wav", "decodable": True}),
        MODULE.CheckResult("audio_transcription", "PASS", "audio_transcription_valid", {"text_present": True, "expected_marker_present": True}),
        MODULE.CheckResult("audio_translation", "PASS", "audio_translation_valid", {"text_present": True, "english_markers_present": True}),
    ]


class MatrixOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.direct = MODULE.TargetConfig("direct", MODULE.DIRECT_BASE_URL, "direct-secret")
        self.gateway = MODULE.TargetConfig("gateway", MODULE.GATEWAY_BASE_URL, "gateway-secret")

    def test_run_target_runs_every_check_in_the_fixed_order(self):
        audio = make_wav()
        requests = []

        def transport(target, method, path, **kwargs):
            requests.append((target.name, path))
            if path == "/v1/models":
                return MODULE.HttpResponse(200, {}, b'{"data":[{"id":"gpt-5-6-pro"},{"id":"gpt-5-6-thinking"},{"id":"gpt-image-2"}]}')
            if path == "/v1/chat/completions":
                if b'"stream":true' in kwargs.get("body", b""):
                    return MODULE.HttpResponse(200, {}, b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\ndata: [DONE]\n\n')
                return MODULE.HttpResponse(200, {}, b'{"choices":[{"message":{"content":"AURORA-CANARY-FILE-OK"}}]}')
            if path == "/v1/responses":
                if b'"stream":true' in kwargs.get("body", b""):
                    return MODULE.HttpResponse(200, {}, b'event: response.created\ndata: {"type":"response.created"}\n\nevent: response.output_text.delta\ndata: {"type":"response.output_text.delta"}\n\nevent: response.completed\ndata: {"type":"response.completed"}\n\ndata: [DONE]\n\n')
                return MODULE.HttpResponse(200, {}, b'{"status":"completed","output":[{"type":"message"}]}')
            if path == "/v1/files":
                return MODULE.HttpResponse(200, {}, b'{"id":"synthetic-file-id"}')
            if path.startswith("/v1/images/"):
                return MODULE.HttpResponse(200, {}, image_json(MODULE.make_test_png()))
            if path == "/v1/audio/speech":
                return MODULE.HttpResponse(200, {"content-type": "audio/wav"}, audio)
            if path == "/v1/audio/transcriptions":
                return MODULE.HttpResponse(200, {}, '{"text":"这是一次能力测试。"}'.encode("utf-8"))
            if path == "/v1/audio/translations":
                return MODULE.HttpResponse(200, {}, b'{"text":"This is a capability test."}')
            self.fail(f"unexpected request: {path}")

        results = MODULE.run_target(self.direct, transport)
        self.assertEqual(tuple(result.name for result in results), MODULE.EXPECTED_CHECKS)
        self.assertTrue(all(result.status == "PASS" for result in results))
        self.assertEqual([path for _, path in requests], [
            "/v1/models", "/v1/chat/completions", "/v1/chat/completions",
            "/v1/responses", "/v1/responses", "/v1/files", "/v1/chat/completions",
            "/v1/images/generations", "/v1/images/edits", "/v1/images/variations",
            "/v1/audio/speech", "/v1/audio/transcriptions", "/v1/audio/translations",
        ])

    def test_run_matrix_blocks_gateway_requests_when_direct_fails(self):
        requests = []

        def transport(target, method, path, **kwargs):
            requests.append(target.name)
            return MODULE.HttpResponse(401, {}, b"")

        results = MODULE.run_matrix(self.direct, self.gateway, target_mode="both", transport=transport)
        self.assertTrue(requests)
        self.assertEqual(set(requests), {"direct"})
        self.assertEqual(
            [(result.name, result.status, result.code, result.details) for result in results["gateway"]],
            [(name, "FAIL", "dependency_failed", {"dependency": "direct"}) for name in MODULE.EXPECTED_CHECKS],
        )

    def test_run_matrix_runs_gateway_only_after_all_direct_checks_pass(self):
        calls = []

        def run_target(target, transport):
            calls.append(target.name)
            return passing_results()

        with mock.patch.object(MODULE, "run_target", side_effect=run_target):
            results = MODULE.run_matrix(self.direct, self.gateway, target_mode="both")
        self.assertEqual(calls, ["direct", "gateway"])
        self.assertEqual(set(results), {"direct", "gateway"})
        self.assertTrue(all(result.status == "PASS" for target in results.values() for result in target))

    def test_run_target_marks_audio_children_as_failed_dependencies(self):
        with mock.patch.object(MODULE, "check_audio_speech", return_value=(MODULE.CheckResult("audio_speech", "FAIL", "connectivity_failed", {}), None)):
            results = MODULE.run_target(self.direct, lambda *args, **kwargs: MODULE.HttpResponse(401, {}, b""))
        self.assertEqual(
            [(result.name, result.code, result.details) for result in results[-3:]],
            [
                ("audio_speech", "connectivity_failed", {}),
                ("audio_transcription", "dependency_failed", {"dependency": "audio_speech"}),
                ("audio_translation", "dependency_failed", {"dependency": "audio_speech"}),
            ],
        )

    def test_cli_reads_gateway_key_only_after_direct_pass_and_redacts_stdout(self):
        events = []

        def read_env(path, key):
            events.append("env")
            return "direct-secret"

        def read_secret(path):
            events.append("gateway-key")
            return "gateway-secret"

        def run_target(target, transport=MODULE.http_request):
            events.append(f"run-{target.name}")
            return passing_results()

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(MODULE, "read_env_value", side_effect=read_env),
            mock.patch.object(MODULE, "read_single_secret", side_effect=read_secret),
            mock.patch.object(MODULE, "run_target", side_effect=run_target),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            exit_code = MODULE.main([
                "--allow-real-api", "--target", "both", "--json",
                "--env-file", str(Path(directory) / "env"),
                "--gateway-key-file", str(Path(directory) / "key"),
            ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(events, ["env", "run-direct", "gateway-key", "run-gateway"])
        self.assertEqual(stderr.getvalue(), "")
        output = stdout.getvalue()
        self.assertIn('"overall":"PASS"', output)
        for forbidden in ("direct-secret", "gateway-secret", MODULE.CHAT_PROMPT, "synthetic-file-id", "https://", "b64_json"):
            self.assertNotIn(forbidden, output)

    def test_cli_returns_one_for_a_completed_failed_direct_matrix(self):
        failed = passing_results()
        failed[0] = MODULE.CheckResult("models", "FAIL", "models_invalid", {})
        with (
            mock.patch.object(MODULE, "read_env_value", return_value="direct-secret"),
            mock.patch.object(MODULE, "run_target", return_value=failed),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = MODULE.main(["--allow-real-api", "--target", "direct", "--json"])
        self.assertEqual(exit_code, 1)

    def test_atomic_write_replaces_only_requested_file_and_rejects_missing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            output.write_bytes(b"old")
            MODULE.atomic_write(output, b'{"schema_version":1}')
            self.assertEqual(output.read_bytes(), b'{"schema_version":1}')
            self.assertEqual({entry.name for entry in root.iterdir()}, {"report.json"})
            with self.assertRaises(MODULE.ProbeError) as raised:
                MODULE.atomic_write(root / "missing" / "report.json", b"new")
            self.assertEqual(raised.exception.code, "output_parent_invalid")

    def test_cli_maps_atomic_replace_failure_to_a_fixed_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            output.write_bytes(b"old")
            stderr = io.StringIO()
            try:
                with (
                    mock.patch.object(MODULE, "read_env_value", return_value="direct-secret"),
                    mock.patch.object(MODULE, "run_target", return_value=passing_results()),
                    mock.patch.object(MODULE.os, "replace", side_effect=OSError("synthetic output failure")),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = MODULE.main([
                        "--allow-real-api", "--target", "direct", "--output", str(output)
                    ])
            except OSError:
                exit_code = None
            self.assertEqual(exit_code, 2)
            self.assertEqual(stderr.getvalue(), "aurora_canary=ERROR code=output_write_failed\n")
            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual({item.name for item in output.parent.iterdir()}, {"report.json"})


class SchemaContractTests(unittest.TestCase):
    def test_report_schema_locks_exact_order_and_sanitized_variants(self):
        with SCHEMA.open(encoding="utf-8") as stream:
            schema = json.load(stream)
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(set(schema["required"]), {"schema_version", "checked_at", "overall", "targets"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]["targets"]["properties"]), {"direct", "gateway"})
        results = schema["$defs"]["results"]
        self.assertEqual((results.get("minItems"), results.get("maxItems"), results.get("items")), (12, 12, False))
        self.assertEqual(
            [item.get("$ref", "").removeprefix("#/$defs/") for item in results.get("prefixItems", [])],
            list(MODULE.EXPECTED_CHECKS),
        )
        check = schema["$defs"].get("check", {})
        self.assertEqual(len(check.get("oneOf", [])), 12)
