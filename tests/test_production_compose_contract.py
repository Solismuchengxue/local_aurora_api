import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductionComposeContractTests(unittest.TestCase):
    def test_aurora_is_the_only_final_session_token_runtime(self):
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("name: aurora-stack", text)
        self.assertIn("container_name: aurora", text)
        self.assertIn(
            "ghcr.io/aurora-develop/aurora@sha256:"
            "358533a8cd6355222297c699338fe6cdc024c6f3d951fb2fb03422350b9b7627",
            text,
        )
        self.assertIn('user: "65532:65532"', text)
        self.assertIn("Authorization: ${AURORA_AUTHORIZATION", text)
        self.assertIn('ENABLE_EXTERNAL_TOKEN: "false"', text)
        self.assertIn("source: ./.secrets/session_tokens.txt", text)
        self.assertIn("target: /home/nonroot/session_tokens.txt", text)
        self.assertIn("read_only: true", text)
        self.assertNotIn("canary", text.lower())

    def test_no_alternative_compose_entry_remains(self):
        compose_files = sorted(path.name for path in ROOT.glob("docker-compose*.yml"))

        self.assertEqual(compose_files, ["docker-compose.yml"])

    def test_new_api_uses_the_approved_rc23_digest(self):
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("# 2026-08-04 目标基线：v1.0.0-rc.23", text)
        self.assertIn(
            "calciumion/new-api@sha256:"
            "bacbbfbed64b4579213316e0ed78415985223bb20c47fbc24572dd7be5aa1695",
            text,
        )
        self.assertEqual(text.count("container_name: new-api"), 1)
        self.assertIn("- ./data/new-api:/data", text)
        self.assertNotIn("calciumion/new-api:latest", text)


if __name__ == "__main__":
    unittest.main()
