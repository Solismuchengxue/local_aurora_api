import importlib.util
import json
from pathlib import Path
import sys
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "check_stack_health.py"
)
SPEC = importlib.util.spec_from_file_location("stack_health", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ResultTests(unittest.TestCase):
    def test_overall_status_uses_worst_result(self):
        results = [
            MODULE.CheckResult("a", "PASS", "ok", {}),
            MODULE.CheckResult("b", "WARN", "warning", {}),
        ]
        self.assertEqual(MODULE.overall_status(results), "WARN")
        results.append(MODULE.CheckResult("c", "FAIL", "failed", {}))
        self.assertEqual(MODULE.overall_status(results), "FAIL")

    def test_safe_text_redacts_known_secret_and_limits_length(self):
        secret = "secret-value"
        value = f"prefix {secret} " + "x" * 500
        result = MODULE.safe_text(value, (secret,), limit=40)
        self.assertNotIn(secret, result)
        self.assertLessEqual(len(result), 40)

    def test_json_and_human_renderers_are_stable(self):
        results = [
            MODULE.CheckResult(
                "containers",
                "PASS",
                "4/4 运行，重启次数均为 0",
                {"running": 4},
            )
        ]
        report = MODULE.build_report(
            results,
            "2026-07-29T12:00:00+08:00",
        )
        parsed = json.loads(MODULE.render_json(report))
        self.assertEqual(parsed["overall"], "PASS")
        self.assertEqual(parsed["checks"][0]["name"], "containers")
        human = MODULE.render_human(report)
        self.assertIn("[通过] 容器", human)
        self.assertIn("总体：通过", human)


if __name__ == "__main__":
    unittest.main()
