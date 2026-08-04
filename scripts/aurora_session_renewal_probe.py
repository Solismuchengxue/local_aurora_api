"""Single-request, sanitized Aurora session-renewal probe."""

import argparse
import json
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


BASE_URL = "http://127.0.0.1:18082"
_RESULT_FIELDS = (
    "classification",
    "http_status",
    "response_is_json",
    "choices_present",
    "message_present",
    "content_nonempty",
)
_ERROR_BODY_LIMIT = 4 * 1024
_SUCCESS_BODY_LIMIT = 64 * 1024
_ACCOUNT_UNAVAILABLE = "no available account of the requested type"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_loopback_opener():
    return build_opener(ProxyHandler({}), _NoRedirectHandler())


_LOOPBACK_OPENER = _build_loopback_opener()


def _result(classification: str, http_status: int | None = None, response_is_json: bool = False,
            choices_present: bool = False, message_present: bool = False,
            content_nonempty: bool = False) -> dict[str, object]:
    return {
        "classification": classification,
        "http_status": http_status,
        "response_is_json": response_is_json,
        "choices_present": choices_present,
        "message_present": message_present,
        "content_nonempty": content_nonempty,
    }


def _read_limited(response, limit: int) -> bytes:
    return response.read(limit)


def _is_fixed_account_error(body: bytes) -> bool:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(decoded, dict) and isinstance(decoded.get("error"), dict) and decoded["error"].get("message") == _ACCOUNT_UNAVAILABLE


def probe_once(authorization: str, opener: Callable | None = None) -> dict[str, object]:
    """Send exactly one synthetic gpt-4o request and return sanitized structure metadata."""
    opener = _LOOPBACK_OPENER.open if opener is None else opener
    payload = json.dumps({
        "model": "gpt-4o",
        "stream": False,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "Reply with OK."}],
    }).encode("utf-8")
    request = Request(
        f"{BASE_URL}/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {authorization}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=60) as response:
            status = response.status
            body = _read_limited(response, _SUCCESS_BODY_LIMIT)
    except HTTPError as error:
        status = error.code
        try:
            body = _read_limited(error, _ERROR_BODY_LIMIT)
        except OSError:
            return _result("unavailable")
        if status == 403:
            return _result("upstream_forbidden", http_status=status)
        if status == 401 or _is_fixed_account_error(body):
            return _result("auth_failed", http_status=status)
        return _result("invalid_response", http_status=status)
    except (URLError, OSError):
        return _result("unavailable")

    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _result("invalid_response", http_status=status)
    if not isinstance(decoded, dict):
        return _result("invalid_response", http_status=status, response_is_json=True)

    choices = decoded.get("choices")
    choices_present = isinstance(choices, list) and bool(choices)
    first_choice = choices[0] if choices_present else None
    message = first_choice.get("message") if isinstance(first_choice, dict) else None
    message_present = isinstance(message, dict)
    content = message.get("content") if message_present else None
    content_nonempty = isinstance(content, str) and bool(content.strip())
    if status == 200 and choices_present and message_present and content_nonempty:
        return _result("pass", status, True, True, True, True)
    return _result("invalid_response", status, True, choices_present, message_present, content_nonempty)


def read_env_value(path: Path, key: str) -> str:
    """Read exactly one strict, unquoted KEY=value entry."""
    prefix = f"{key}="
    values = []
    invalid_target_entry = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith(prefix):
            values.append(line[len(prefix):])
        elif key in stripped:
            invalid_target_entry = True
    if invalid_target_entry or len(values) != 1:
        raise ValueError(f"expected exactly one non-empty {key} entry")
    value = values[0]
    if not value or any(character.isspace() for character in value) or "\x00" in value \
            or "#" in value or "$" in value or "'" in value or '"' in value:
        raise ValueError(f"expected exactly one non-empty {key} entry")
    return value


def render_json(result: dict[str, object]) -> str:
    """Serialize only the fixed allowlisted fields."""
    return json.dumps({field: result.get(field) for field in _RESULT_FIELDS}, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    """Require --allow-real-api, read --env-file (default .env.canary), and return 0 only for pass."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-real-api", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env.canary"))
    parser.add_argument("--json", action="store_true", help="compatibility flag; output is always JSON")
    args = parser.parse_args(argv)
    if not args.allow_real_api:
        return 2
    try:
        authorization = read_env_value(args.env_file, "AURORA_CANARY_AUTHORIZATION")
    except (OSError, ValueError):
        print(render_json(_result("unavailable")))
        return 1
    result = probe_once(authorization)
    print(render_json(result))
    return 0 if result["classification"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
