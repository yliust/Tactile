import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "feishu_fast.py"
SPEC = importlib.util.spec_from_file_location("feishu_fast", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
feishu_fast = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = feishu_fast
SPEC.loader.exec_module(feishu_fast)


class FeishuFastTimingTests(unittest.TestCase):
    def test_wait_seconds_enforces_one_second_minimum(self):
        self.assertEqual(feishu_fast.wait_seconds(SimpleNamespace(wait_ms=100)), 1.0)
        self.assertEqual(feishu_fast.wait_seconds(SimpleNamespace(wait_ms=1000)), 1.0)
        self.assertEqual(feishu_fast.wait_seconds(SimpleNamespace(wait_ms=1500)), 1.5)
        self.assertEqual(feishu_fast.wait_seconds(SimpleNamespace(), default_ms=100), 1.0)

    def test_paste_text_keeps_one_second_between_input_actions(self):
        ctx = feishu_fast.FastContext(
            repo=Path("/tmp/repo"),
            ensure_products=lambda _repo, _products: None,
            debug_tool=lambda _repo, name: Path("/tmp") / name,
            product_cache={"InputControllerTool": "/tmp/InputControllerTool"},
        )
        commands = []
        sleeps = []

        def fake_run(cmd, *, timeout=10, input_text=None, check=True):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="ok")

        original_run = ctx.run
        original_sleep = feishu_fast.time.sleep
        try:
            ctx.run = fake_run
            feishu_fast.time.sleep = sleeps.append

            ctx.paste_text("hello", replace_existing=True, delay=0.05)
        finally:
            ctx.run = original_run
            feishu_fast.time.sleep = original_sleep

        self.assertIn(["/tmp/InputControllerTool", "keypress", "cmd+a"], commands)
        self.assertIn(["/tmp/InputControllerTool", "keypress", "cmd+v"], commands)
        self.assertEqual(sleeps, [1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
