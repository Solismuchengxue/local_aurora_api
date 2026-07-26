import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


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
        self.now = 1_700_000_000
        self.current = make_token(self.now + 48 * 3600, "old")
        self.new = make_token(self.now + 7 * 24 * 3600, "new")
        MODULE.atomic_write_secret(
            self.secrets / "access_tokens.txt", self.current
        )
        MODULE.atomic_write_secret(
            self.secrets / "session_tokens.txt", "session-secret"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_skips_when_token_is_outside_threshold(self):
        calls = []
        result = MODULE.refresh_once(
            self.root,
            threshold_seconds=24 * 3600,
            force=False,
            now=self.now,
            exchange_fn=lambda value: calls.append(value),
            restart_fn=lambda: calls.append("restart"),
            validate_fn=lambda: calls.append("validate"),
        )
        self.assertEqual(result, "skipped")
        self.assertEqual(calls, [])
        self.assertEqual(
            MODULE.read_single_secret(self.secrets / "access_tokens.txt"),
            self.current,
        )

    def test_refreshes_atomically_and_keeps_one_backup(self):
        calls = []
        result = MODULE.refresh_once(
            self.root,
            threshold_seconds=72 * 3600,
            force=False,
            now=self.now,
            exchange_fn=lambda value: (
                calls.append(("exchange", value)) or self.new
            ),
            restart_fn=lambda: calls.append("restart"),
            validate_fn=lambda: calls.append("validate"),
        )
        self.assertEqual(result, "refreshed")
        self.assertEqual(
            MODULE.read_single_secret(self.secrets / "access_tokens.txt"),
            self.new,
        )
        self.assertEqual(
            MODULE.read_single_secret(
                self.secrets / "access_tokens.previous.txt"
            ),
            self.current,
        )
        self.assertEqual(
            calls,
            [("exchange", "session-secret"), "restart", "validate"],
        )
        self.assertEqual(
            (self.secrets / "access_tokens.txt").stat().st_mode & 0o777,
            0o600,
        )

    def test_restores_previous_token_when_validation_fails(self):
        restarts = []
        validations = []

        def validate():
            validations.append(True)
            if len(validations) == 1:
                raise MODULE.RefreshError("new token failed")

        with self.assertRaisesRegex(
            MODULE.RefreshError, "previous token restored"
        ):
            MODULE.refresh_once(
                self.root,
                threshold_seconds=72 * 3600,
                force=False,
                now=self.now,
                exchange_fn=lambda value: self.new,
                restart_fn=lambda: restarts.append(True),
                validate_fn=validate,
            )
        self.assertEqual(len(restarts), 2)
        self.assertEqual(len(validations), 2)
        self.assertEqual(
            MODULE.read_single_secret(self.secrets / "access_tokens.txt"),
            self.current,
        )


if __name__ == "__main__":
    unittest.main()
