import base64
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "refresh_chatgpt_access_token.py"
)
SPEC = importlib.util.spec_from_file_location("token_refresh", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def make_token(exp: int, marker: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"exp": exp, "marker": marker}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.signature-{marker}"


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.secrets = self.root / ".secrets"
        self.secrets.mkdir(mode=0o700)
        database_dir = self.root / "data" / "new-api"
        database_dir.mkdir(parents=True)
        self.database = database_dir / "one-api.db"
        self.now = 1_700_000_000
        self.current = make_token(self.now + 48 * 3600, "old")
        self.new = make_token(self.now + 7 * 24 * 3600, "new")
        with sqlite3.connect(self.database) as database:
            database.execute(
                "CREATE TABLE channels (id INTEGER PRIMARY KEY, key TEXT, status INTEGER)"
            )
            database.execute(
                "INSERT INTO channels (id, key, status) VALUES (1, ?, 1)",
                (self.current,),
            )
        MODULE.atomic_write_secret(
            self.secrets / "session_tokens.txt", "session-secret"
        )

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self, calls, validate_gateway=lambda: None):
        return MODULE.refresh_once(
            self.root,
            channel_id=1,
            threshold_seconds=72 * 3600,
            force=False,
            now=self.now,
            exchange_fn=lambda value: (
                calls.append(("exchange", value)) or self.new
            ),
            validate_upstream_fn=lambda token: calls.append(
                ("validate_upstream", token)
            ),
            replace_channel_fn=lambda old, new: (
                calls.append(("replace", old, new))
                or MODULE.replace_channel_token(self.root, 1, old, new)
            ),
            restart_fn=lambda: calls.append("restart"),
            validate_gateway_fn=validate_gateway,
        )

    def test_skips_when_token_is_outside_threshold(self):
        later = make_token(self.now + 7 * 24 * 3600, "later")
        MODULE.replace_channel_token(self.root, 1, self.current, later)
        calls = []
        result = self.refresh(calls)
        self.assertEqual(result, "skipped")
        self.assertEqual(calls, [])
        self.assertEqual(MODULE.read_channel_token(self.root, 1), later)

    def test_refreshes_channel_and_keeps_secret_backups(self):
        calls = []
        result = self.refresh(calls)
        self.assertEqual(result, "refreshed")
        self.assertEqual(MODULE.read_channel_token(self.root, 1), self.new)
        self.assertEqual(
            MODULE.read_single_secret(
                self.secrets / "access_tokens.previous.txt"
            ),
            self.current,
        )
        self.assertEqual(
            MODULE.read_single_secret(self.secrets / "access_tokens.txt"),
            self.new,
        )
        self.assertEqual(
            calls,
            [
                ("exchange", "session-secret"),
                ("validate_upstream", self.new),
                ("replace", self.current, self.new),
                "restart",
            ],
        )

    def test_restores_previous_channel_token_when_gateway_fails(self):
        calls = []
        validations = []

        def validate_gateway():
            validations.append(True)
            if len(validations) == 1:
                raise MODULE.RefreshError("new token failed")

        with self.assertRaisesRegex(
            MODULE.RefreshError, "previous token restored"
        ):
            self.refresh(calls, validate_gateway)
        self.assertEqual(MODULE.read_channel_token(self.root, 1), self.current)
        self.assertEqual(calls.count("restart"), 2)
        self.assertEqual(len(validations), 2)

    def test_channel_update_refuses_concurrent_change(self):
        with self.assertRaisesRegex(
            MODULE.RefreshError, "changed concurrently"
        ):
            MODULE.replace_channel_token(
                self.root,
                1,
                "not-the-current-token",
                self.new,
            )
        self.assertEqual(MODULE.read_channel_token(self.root, 1), self.current)

    def test_chat_validation_falls_back_to_thinking_model(self):
        chat_models = []
        chat_prompts = []

        def request_json(url, authorization, *, payload=None, timeout=30):
            if payload is None:
                return {
                    "data": [
                        {"id": "gpt-5-6-pro"},
                        {"id": "gpt-5-6-thinking"},
                    ]
                }
            chat_models.append(payload["model"])
            chat_prompts.append(payload["messages"][0]["content"])
            if payload["model"] == "gpt-5-6-pro":
                return {"choices": []}
            return {
                "choices": [
                    {"message": {"content": "OK"}, "finish_reason": "stop"}
                ]
            }

        with mock.patch.object(MODULE, "request_json", side_effect=request_json):
            with mock.patch.object(MODULE.time, "sleep"):
                MODULE.validate_chat_api("http://example.invalid", "test-key")

        self.assertEqual(
            chat_models,
            ["gpt-5-6-pro", "gpt-5-6-thinking"],
        )
        self.assertEqual(len(set(chat_prompts)), 2)
        self.assertTrue(
            all("AURORA-VERIFY-" in prompt for prompt in chat_prompts)
        )

    def test_chat_validation_accepts_valid_empty_completion(self):
        chat_models = []

        def request_json(url, authorization, *, payload=None, timeout=30):
            if payload is None:
                return {"data": [{"id": "gpt-5-6-pro"}]}
            chat_models.append(payload["model"])
            return {
                "choices": [
                    {"message": {"content": ""}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 18,
                    "completion_tokens": 0,
                    "total_tokens": 18,
                },
            }

        with mock.patch.object(MODULE, "request_json", side_effect=request_json):
            with mock.patch.object(MODULE.time, "sleep"):
                MODULE.validate_chat_api("http://example.invalid", "test-key")

        self.assertEqual(chat_models, ["gpt-5-6-pro"])


if __name__ == "__main__":
    unittest.main()
