from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.canary.yml"
ENV_EXAMPLE = ROOT / ".env.canary.example"


class CanaryComposeContractTests(unittest.TestCase):
    def test_canary_compose_is_isolated_and_fail_closed(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("name: aurora-capability-canary", text)
        self.assertRegex(text, r"(?m)^  aurora-canary:$")
        self.assertRegex(text, r"(?m)^  new-api-canary:$")
        self.assertNotRegex(text, r"(?m)^  (aurora|new-api|mihomo|dashboard):$")
        self.assertIn('"127.0.0.1:18080:8080"', text)
        self.assertIn('"127.0.0.1:13000:3000"', text)
        self.assertIn('ENABLE_EXTERNAL_TOKEN: "false"', text)
        self.assertIn("target: /session_tokens.txt", text)
        self.assertIn("read_only: true", text)
        self.assertNotIn("/vol1/1000/Solis_Aurora_Gateway/data/new-api", text)
        self.assertNotIn("/var/run/docker.sock", text)
        self.assertNotIn("network_mode: host", text)
        self.assertIn("name: aurora-stack_default", text)
        self.assertIn("external: true", text)

    def test_candidate_image_is_required_and_new_api_stays_on_baseline(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn(
            "image: ${AURORA_CANARY_IMAGE:?set an approved immutable Aurora digest}",
            text,
        )
        self.assertIn(
            "calciumion/new-api@sha256:428018a37c0b26c163a3367c18401161707cd0e08d0f26a3dde9ff0caa05e34c",
            text,
        )
        self.assertNotIn(":latest", text)

    def test_example_lists_names_only_and_real_file_is_ignored(self):
        example = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertEqual(
            [line for line in example.splitlines() if line and not line.startswith("#")],
            [
                "AURORA_CANARY_IMAGE=",
                "AURORA_CANARY_AUTHORIZATION=",
                "NEW_API_CANARY_SESSION_SECRET=",
            ],
        )
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertRegex(ignore, r"(?m)^/\.env\.canary$")


if __name__ == "__main__":
    unittest.main()
