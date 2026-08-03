#!/usr/bin/env python3
"""Run an explicitly authorized capability matrix against isolated canary ports."""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import sys
import struct
import urllib.parse
import urllib.error
import urllib.request
import zlib
from typing import Callable, Mapping


DIRECT_BASE_URL = "http://127.0.0.1:18080"
GATEWAY_BASE_URL = "http://127.0.0.1:13000"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024
STATUS_ORDER = {"PASS": 0, "FAIL": 1}
EXPECTED_CHECKS = (
    "models",
    "chat_nonstream",
    "chat_stream",
    "responses_nonstream",
    "responses_stream",
    "files",
    "image_generation",
    "image_edit",
    "image_variation",
    "audio_speech",
    "audio_transcription",
    "audio_translation",
)
TRANSPORT_ERROR_CODES = {
    "auth_failed",
    "upstream_forbidden",
    "route_missing",
    "rate_limited",
    "upstream_failed",
    "http_failed",
    "timeout",
    "connectivity_failed",
    "response_too_large",
}
PASS_DETAIL_KEYS = {
    "models_valid": {"count"},
    "chat_nonstream_valid": {"content_present"},
    "chat_stream_valid": {"chunks", "done"},
    "responses_nonstream_valid": {"completed", "output_count"},
    "responses_stream_valid": {"created", "output_seen", "completed", "done"},
    "files_valid": {"upload_accepted", "file_id_present", "answer_present"},
    "image_generation_valid": {"bytes", "media_type", "decodable"},
    "image_edit_valid": {"bytes", "media_type", "decodable"},
    "image_variation_valid": {"bytes", "media_type", "decodable"},
    "audio_speech_valid": {"bytes", "media_type", "decodable"},
    "audio_transcription_valid": {"text_present", "expected_marker_present"},
    "audio_translation_valid": {"text_present", "english_markers_present"},
}
STRUCTURE_ERROR_CODES = {
    "models_invalid",
    "chat_nonstream_invalid",
    "chat_empty",
    "chat_stream_invalid",
    "responses_nonstream_invalid",
    "responses_stream_invalid",
    "files_invalid",
    "image_payload_invalid",
    "image_url_not_accepted",
    "audio_payload_invalid",
    "transcription_mismatch",
    "translation_mismatch",
    "dependency_failed",
}
FAIL_DETAIL_KEYS = {"dependency_failed": {"dependency"}}
MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "audio/wav",
    "audio/mpeg",
    "audio/ogg",
    "audio/opus",
    "audio/flac",
    "audio/aac",
    "audio/webm",
}
ALLOWED_PATHS = {
    "/v1/models",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/files",
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/images/variations",
    "/v1/audio/speech",
    "/v1/audio/transcriptions",
    "/v1/audio/translations",
}
CHAT_PROMPT = "Reply with exactly: AURORA-CANARY-OK"
RESPONSES_INPUT = "Reply with exactly: AURORA-CANARY-OK"
FILE_MARKER = "AURORA-CANARY-FILE-OK"
IMAGE_GENERATION_PROMPT = "A small synthetic blue square on a plain white background."
IMAGE_EDIT_PROMPT = "Keep the synthetic image unchanged."
REQUIRED_MODEL_IDS = {"gpt-5-6-pro", "gpt-5-6-thinking", "gpt-image-2"}


class ProbeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    code: str
    details: dict[str, object]

    def __post_init__(self) -> None:
        if self.status not in STATUS_ORDER:
            raise ValueError("invalid status")
        if not self.name.isascii() or not self.code.isascii():
            raise ValueError("invalid result identifier")


@dataclass(frozen=True)
class TargetConfig:
    name: str
    base_url: str
    authorization: str


Transport = Callable[..., HttpResponse]


def validate_canary_url(url: str, target: str) -> str:
    if target not in {"direct", "gateway"}:
        raise ProbeError("unsafe_target")
    expected = DIRECT_BASE_URL if target == "direct" else GATEWAY_BASE_URL
    parsed = urllib.parse.urlsplit(url)
    if url.rstrip("/") != expected or parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ProbeError("unsafe_target")
    return expected


def http_request(
    target: TargetConfig,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 30,
) -> HttpResponse:
    validate_canary_url(target.base_url, target.name)
    if method not in {"GET", "POST"} or path not in ALLOWED_PATHS:
        raise ProbeError("unsafe_request")
    if body is not None and len(body) > MAX_REQUEST_BYTES:
        raise ProbeError("request_too_large")
    headers = {"Authorization": f"Bearer {target.authorization}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(target.base_url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ProbeError("response_too_large")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return HttpResponse(response.status, response_headers, payload)
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, {}, b"")
    except TimeoutError as exc:
        raise ProbeError("timeout") from exc
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise ProbeError("connectivity_failed") from exc


def require_success(response: HttpResponse) -> HttpResponse:
    mapping = {401: "auth_failed", 403: "upstream_forbidden", 404: "route_missing", 429: "rate_limited"}
    if 200 <= response.status < 300:
        return response
    if response.status >= 500:
        raise ProbeError("upstream_failed")
    raise ProbeError(mapping.get(response.status, "http_failed"))


def decode_json(response: HttpResponse) -> dict[str, object]:
    if len(response.body) > MAX_JSON_BYTES:
        raise ProbeError("json_too_large")
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError("json_invalid") from exc
    if not isinstance(value, dict):
        raise ProbeError("json_invalid")
    return value


def parse_sse(body: bytes) -> list[tuple[str, dict[str, object]]]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProbeError("response_too_large")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeError("sse_invalid") from exc
    events: list[tuple[str, dict[str, object]]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block:
            continue
        event_name: str | None = None
        data: str | None = None
        for line in block.split("\n"):
            if line.startswith("event: ") and event_name is None:
                event_name = line[7:]
            elif line.startswith("data: ") and data is None:
                data = line[6:]
            else:
                raise ProbeError("sse_invalid")
        if data is None:
            raise ProbeError("sse_invalid")
        if data == "[DONE]":
            if event_name is not None:
                raise ProbeError("sse_invalid")
            events.append(("done", {}))
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProbeError("sse_invalid") from exc
        if not isinstance(payload, dict):
            raise ProbeError("sse_invalid")
        payload_type = payload.get("type")
        if event_name is None:
            event_name = payload_type if isinstance(payload_type, str) else "message"
        if not event_name:
            raise ProbeError("sse_invalid")
        events.append((event_name, payload))
    return events


def request_json(
    target: TargetConfig,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    transport: Transport = http_request,
) -> dict[str, object]:
    return decode_json(require_success(transport(target, method, path, body=body, content_type="application/json" if body else None)))


def _failure(name: str, structure_code: str, error: ProbeError) -> CheckResult:
    code = error.code if error.code in TRANSPORT_ERROR_CODES | STRUCTURE_ERROR_CODES else structure_code
    return CheckResult(name, "FAIL", code, {})


def _json_body(value: dict[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def make_test_png() -> bytes:
    """Return a deterministic opaque 64x64 RGB PNG without metadata."""
    width = height = 64
    raw = b"".join(b"\x00" + b"\x2d\x72\xd2" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + _png_chunk(b"IEND", b"")
    )


def _is_mime_token(value: str) -> bool:
    return bool(value) and all(char.isascii() and (char.isalnum() or char in "!#$%&'*+-.^_`|~") for char in value)


def encode_multipart(
    *,
    fields: Mapping[str, str],
    files: Mapping[str, tuple[str, str, bytes]],
    boundary: str = "aurora-canary-boundary",
) -> tuple[str, bytes]:
    if not _is_mime_token(boundary):
        raise ProbeError("multipart_invalid")
    chunks: list[bytes] = []
    marker = f"--{boundary}".encode("ascii")
    for name in sorted(fields):
        value = fields[name]
        if not _is_mime_token(name) or not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ProbeError("multipart_invalid")
        chunks.extend((marker, f'Content-Disposition: form-data; name="{name}"'.encode("ascii"), b"", value.encode("utf-8")))
    for name in sorted(files):
        filename, media_type, payload = files[name]
        if (
            not _is_mime_token(name)
            or not filename.isascii()
            or not filename
            or any(char in filename for char in "\\/\r\n\"")
            or media_type.count("/") != 1
            or not all(_is_mime_token(part) for part in media_type.split("/"))
            or not isinstance(payload, bytes)
        ):
            raise ProbeError("multipart_invalid")
        chunks.extend((
            marker,
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode("ascii"),
            f"Content-Type: {media_type}".encode("ascii"),
            b"",
            payload,
        ))
    body = b"\r\n".join(chunks) + b"\r\n" + marker + b"--\r\n"
    if len(body) > MAX_REQUEST_BYTES:
        raise ProbeError("request_too_large")
    return f"multipart/form-data; boundary={boundary}", body


def decode_image_result(payload: dict[str, object]) -> dict[str, object]:
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ProbeError("image_payload_invalid")
    item = data[0]
    if "url" in item:
        raise ProbeError("image_url_not_accepted")
    if set(item) != {"b64_json"}:
        raise ProbeError("image_payload_invalid")
    encoded = item.get("b64_json")
    if not isinstance(encoded, str):
        raise ProbeError("image_payload_invalid")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProbeError("image_payload_invalid") from exc
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif image.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif len(image) >= 12 and image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        media_type = "image/webp"
    else:
        raise ProbeError("image_payload_invalid")
    return {"bytes": len(image), "media_type": media_type, "decodable": True}


def _check_image(
    name: str,
    path: str,
    target: TargetConfig,
    *,
    body: bytes,
    content_type: str,
    transport: Transport,
) -> CheckResult:
    try:
        response = require_success(transport(target, "POST", path, body=body, content_type=content_type))
        return CheckResult(name, "PASS", f"{name}_valid", decode_image_result(decode_json(response)))
    except ProbeError as exc:
        return _failure(name, "image_payload_invalid", exc)


def check_image_generation(target: TargetConfig, transport: Transport = http_request) -> CheckResult:
    return _check_image(
        "image_generation", "/v1/images/generations", target,
        body=_json_body({"model": "gpt-image-2", "prompt": IMAGE_GENERATION_PROMPT, "n": 1, "size": "1024x1024", "response_format": "b64_json"}),
        content_type="application/json", transport=transport,
    )


def _image_multipart(*, include_prompt: bool) -> tuple[str, bytes]:
    fields = {"model": "gpt-image-2", "n": "1", "size": "1024x1024", "response_format": "b64_json"}
    if include_prompt:
        fields["prompt"] = IMAGE_EDIT_PROMPT
    return encode_multipart(
        fields=fields,
        files={"image": ("aurora-canary.png", "image/png", make_test_png())},
    )


def check_image_edit(target: TargetConfig, transport: Transport = http_request) -> CheckResult:
    content_type, body = _image_multipart(include_prompt=True)
    return _check_image("image_edit", "/v1/images/edits", target, body=body, content_type=content_type, transport=transport)


def check_image_variation(target: TargetConfig, transport: Transport = http_request) -> CheckResult:
    content_type, body = _image_multipart(include_prompt=False)
    return _check_image("image_variation", "/v1/images/variations", target, body=body, content_type=content_type, transport=transport)


def check_files(target: TargetConfig, transport: Transport = http_request) -> CheckResult:
    try:
        content_type, upload_body = encode_multipart(
            fields={"purpose": "assistants"},
            files={"file": ("aurora-canary.txt", "text/plain", b"AURORA CANARY SYNTHETIC FILE")},
        )
        upload = decode_json(require_success(transport(target, "POST", "/v1/files", body=upload_body, content_type=content_type)))
        file_id = upload.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise ProbeError("files_invalid")
        chat_body = _json_body({
            "model": "gpt-5-6-pro",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": f"Read the file and reply with exactly: {FILE_MARKER}"},
                {"type": "input_file", "file_id": file_id},
            ]}],
        })
        chat = decode_json(require_success(transport(target, "POST", "/v1/chat/completions", body=chat_body, content_type="application/json")))
        choices = chat.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProbeError("files_invalid")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str) or FILE_MARKER not in message["content"]:
            raise ProbeError("files_invalid")
        return CheckResult("files", "PASS", "files_valid", {"upload_accepted": True, "file_id_present": True, "answer_present": True})
    except ProbeError as exc:
        return _failure("files", "files_invalid", exc)


def check_models(target: TargetConfig, transport: Transport = http_request) -> CheckResult:
    try:
        payload = request_json(target, "GET", "/v1/models", transport=transport)
        models = payload.get("data")
        if not isinstance(models, list):
            raise ProbeError("models_invalid")
        model_ids = {item.get("id") for item in models if isinstance(item, dict) and isinstance(item.get("id"), str)}
        if not REQUIRED_MODEL_IDS <= model_ids:
            raise ProbeError("models_invalid")
        return CheckResult("models", "PASS", "models_valid", {"count": len(models)})
    except ProbeError as exc:
        return _failure("models", "models_invalid", exc)


def check_chat_nonstream(target: TargetConfig, transport: Transport = http_request) -> CheckResult:
    try:
        payload = request_json(target, "POST", "/v1/chat/completions", body=_json_body({"model": "gpt-5-6-pro", "messages": [{"role": "user", "content": CHAT_PROMPT}]}), transport=transport)
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProbeError("chat_nonstream_invalid")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ProbeError("chat_nonstream_invalid")
        if not message["content"]:
            return CheckResult("chat_nonstream", "FAIL", "chat_empty", {})
        return CheckResult("chat_nonstream", "PASS", "chat_nonstream_valid", {"content_present": True})
    except ProbeError as exc:
        return _failure("chat_nonstream", "chat_nonstream_invalid", exc)


def check_chat_stream(target: TargetConfig, transport: Transport = http_request) -> CheckResult:
    try:
        response = require_success(transport(target, "POST", "/v1/chat/completions", body=_json_body({"model": "gpt-5-6-pro", "messages": [{"role": "user", "content": CHAT_PROMPT}], "stream": True}), content_type="application/json"))
        events = parse_sse(response.body)
        chunks = sum(
            1
            for event, payload in events
            if event != "done"
            and isinstance(payload.get("choices"), list)
            and payload["choices"]
            and any(isinstance(choice, dict) and isinstance(choice.get("delta"), dict) for choice in payload["choices"])
        )
        done = bool(events) and events[-1][0] == "done"
        if not chunks or not done:
            raise ProbeError("chat_stream_invalid")
        return CheckResult("chat_stream", "PASS", "chat_stream_valid", {"chunks": chunks, "done": True})
    except ProbeError as exc:
        return _failure("chat_stream", "chat_stream_invalid", exc)


def check_responses_nonstream(target: TargetConfig, transport: Transport = http_request) -> CheckResult:
    try:
        payload = request_json(target, "POST", "/v1/responses", body=_json_body({"model": "gpt-5-6-pro", "input": RESPONSES_INPUT}), transport=transport)
        output = payload.get("output")
        if payload.get("status") != "completed" or not isinstance(output, list) or not output:
            raise ProbeError("responses_nonstream_invalid")
        return CheckResult("responses_nonstream", "PASS", "responses_nonstream_valid", {"completed": True, "output_count": len(output)})
    except ProbeError as exc:
        return _failure("responses_nonstream", "responses_nonstream_invalid", exc)


def check_responses_stream(target: TargetConfig, transport: Transport = http_request) -> CheckResult:
    try:
        response = require_success(transport(target, "POST", "/v1/responses", body=_json_body({"model": "gpt-5-6-pro", "input": RESPONSES_INPUT, "stream": True}), content_type="application/json"))
        events = parse_sse(response.body)
        names = []
        for name, payload in events:
            if name == "done":
                names.append(name)
            elif name in {"response.created", "response.completed"} or name.startswith("response.output"):
                if not isinstance(payload.get("type"), str) or payload["type"] != name:
                    raise ProbeError("responses_stream_invalid")
                names.append(name)
        created = "response.created" in names
        output_seen = any(name.startswith("response.output") for name in names)
        completed = "response.completed" in names
        done = bool(events) and events[-1][0] == "done"
        if not (created and output_seen and completed and done and names.index("response.created") < next(index for index, name in enumerate(names) if name.startswith("response.output")) < names.index("response.completed")):
            raise ProbeError("responses_stream_invalid")
        return CheckResult("responses_stream", "PASS", "responses_stream_valid", {"created": True, "output_seen": True, "completed": True, "done": True})
    except ProbeError as exc:
        return _failure("responses_stream", "responses_stream_invalid", exc)


def read_env_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ProbeError("credential_unavailable") from exc
    values = []
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            values.append(stripped[len(prefix):])
    if len(values) != 1 or not values[0].strip() or "\x00" in values[0]:
        raise ProbeError("credential_invalid")
    return values[0]


def read_single_secret(path: Path) -> str:
    try:
        values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError) as exc:
        raise ProbeError("credential_unavailable") from exc
    if len(values) != 1 or "\x00" in values[0]:
        raise ProbeError("credential_invalid")
    return values[0]


def _validate_result(result: CheckResult) -> None:
    if result.name not in EXPECTED_CHECKS:
        raise ValueError("invalid check name")
    if result.status == "PASS":
        expected_details = PASS_DETAIL_KEYS.get(result.code)
    elif result.code in TRANSPORT_ERROR_CODES:
        expected_details = set()
    else:
        expected_details = FAIL_DETAIL_KEYS.get(result.code)
        if expected_details is None and result.code in STRUCTURE_ERROR_CODES:
            expected_details = set()
    if expected_details is None or set(result.details) != expected_details:
        raise ValueError("invalid result details")
    for key, value in result.details.items():
        if key in {"count", "chunks", "output_count", "bytes"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("invalid result details")
        elif key == "media_type":
            if value not in MEDIA_TYPES:
                raise ValueError("invalid result details")
        elif key == "dependency":
            if value not in {"direct", "audio_speech"}:
                raise ValueError("invalid result details")
        elif not isinstance(value, bool):
            raise ValueError("invalid result details")


def _validate_report(report: Mapping[str, object]) -> dict[str, object]:
    if set(report) != {"schema_version", "checked_at", "overall", "targets"}:
        raise ValueError("invalid report")
    if report["schema_version"] != 1 or report["overall"] not in STATUS_ORDER:
        raise ValueError("invalid report")
    checked_at = report["checked_at"]
    if not isinstance(checked_at, str) or not checked_at.endswith("Z"):
        raise ValueError("invalid report")
    try:
        datetime.fromisoformat(f"{checked_at[:-1]}+00:00")
    except ValueError as exc:
        raise ValueError("invalid report") from exc
    targets = report["targets"]
    if not isinstance(targets, Mapping) or not targets or set(targets) - {"direct", "gateway"}:
        raise ValueError("invalid report")
    sanitized_targets: dict[str, list[dict[str, object]]] = {}
    statuses: list[str] = []
    for name, results in targets.items():
        if not isinstance(results, list):
            raise ValueError("invalid report")
        sanitized_results = []
        for item in results:
            if not isinstance(item, Mapping) or set(item) != {"name", "status", "code", "details"}:
                raise ValueError("invalid report")
            if not isinstance(item["name"], str) or not isinstance(item["status"], str):
                raise ValueError("invalid report")
            if not isinstance(item["code"], str) or not isinstance(item["details"], dict):
                raise ValueError("invalid report")
            result = CheckResult(item["name"], item["status"], item["code"], item["details"])
            _validate_result(result)
            sanitized_results.append(asdict(result))
            statuses.append(result.status)
        sanitized_targets[name] = sanitized_results
    expected_overall = "PASS" if statuses and all(status == "PASS" for status in statuses) else "FAIL"
    if report["overall"] != expected_overall:
        raise ValueError("invalid report")
    return {
        "schema_version": 1,
        "checked_at": checked_at,
        "overall": expected_overall,
        "targets": sanitized_targets,
    }


def build_report(
    targets: Mapping[str, list[CheckResult]],
    checked_at: datetime | None = None,
) -> dict[str, object]:
    if not targets or set(targets) - {"direct", "gateway"}:
        raise ValueError("invalid report targets")
    for results in targets.values():
        for result in results:
            _validate_result(result)
    timestamp = checked_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware")
    return _validate_report({
        "schema_version": 1,
        "checked_at": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "overall": "PASS" if all(result.status == "PASS" for results in targets.values() for result in results) else "FAIL",
        "targets": {name: [asdict(result) for result in results] for name, results in targets.items()},
    })


def serialize_report(report: Mapping[str, object]) -> str:
    encoded = json.dumps(_validate_report(report), ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise ProbeError("report_too_large")
    return encoded.decode("utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-real-api", action="store_true")
    return parser.parse_args(argv)


def run_matrix() -> dict[str, list[CheckResult]]:
    return {}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.allow_real_api:
        print("aurora_canary=ERROR code=real_api_not_authorized", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
