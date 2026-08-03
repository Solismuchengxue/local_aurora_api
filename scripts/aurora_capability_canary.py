#!/usr/bin/env python3
"""Run an explicitly authorized capability matrix against isolated canary ports."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import urllib.parse
from typing import Callable, Mapping


DIRECT_BASE_URL = "http://127.0.0.1:18080"
GATEWAY_BASE_URL = "http://127.0.0.1:13000"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
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
    if len(values) != 1 or not values[0] or "\x00" in values[0]:
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
    return {
        "schema_version": 1,
        "checked_at": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "overall": "PASS" if all(result.status == "PASS" for results in targets.values() for result in results) else "FAIL",
        "targets": {name: [asdict(result) for result in results] for name, results in targets.items()},
    }


def serialize_report(report: Mapping[str, object]) -> str:
    encoded = json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
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
