import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
TRACE_PATH = SKILL_ROOT / "scripts" / "utils" / "tactile_trace.py"
INTERFACE_PATH = SKILL_ROOT / "scripts" / "windows_interface.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tactile_trace = load_module("tactile_windows_trace", TRACE_PATH)
windows_interface = load_module("tactile_windows_interface_for_trace_tests", INTERFACE_PATH)


class WindowsTactileTraceTests(unittest.TestCase):
    def test_trace_schema_metrics_and_outcome(self):
        run_log = {
            "target": {"identifier": "Calculator", "hwnd": 123},
            "instruction": "inspect calculator",
            "final_status": "finished",
            "steps": [
                {
                    "step": 1,
                    "target": {"app": "Calculator", "hwnd": 123},
                    "uia_view": "control",
                    "accessibility_hint": "normal",
                    "element_count_sent_to_llm": 4,
                    "plan": {"status": "continue", "summary": "click", "actions": [{"type": "click", "element_id": "e1"}]},
                    "execution_results": [
                        {"ok": True, "mode": "uia_coordinate_click", "action": {"type": "click", "element_id": "e1"}}
                    ],
                },
                {
                    "step": 2,
                    "target": {"app": "Calculator", "hwnd": 123},
                    "uia_view": "control",
                    "plan": {"status": "finished", "summary": "finish", "actions": [{"type": "click", "x": 5, "y": 8}]},
                    "execution_results": [
                        {
                            "ok": True,
                            "mode": "coordinate",
                            "action": {"type": "click", "x": 5, "y": 8},
                            "point": {"x": 5, "y": 8},
                            "verification": {"matched": True, "ocr_lines": ["ok"]},
                        }
                    ],
                },
            ],
        }

        trace = tactile_trace.build_trace(run_log, platform="windows")

        self.assertEqual(trace["schema_version"], 1)
        self.assertEqual(trace["kind"], "tactile_trace")
        self.assertEqual(trace["platform"], "windows")
        self.assertTrue(trace["outcome"]["verified"])
        self.assertEqual(trace["metrics"]["action_count"], 2)
        self.assertEqual(trace["metrics"]["uia_action_count"], 1)
        self.assertEqual(trace["metrics"]["coordinate_action_count"], 1)
        self.assertEqual(trace["metrics"]["passed_verification_count"], 1)
        self.assertEqual(trace["steps"][1]["execution"][0]["coordinate_source"], "coordinate")

    def test_plan_log_outputs_trace_summary_and_supports_old_logs(self):
        trace = tactile_trace.build_trace(
            {
                "target": {"identifier": "Calculator", "hwnd": 123},
                "instruction": "inspect",
                "final_status": "finished",
                "steps": [
                    {
                        "step": 1,
                        "plan": {"summary": "done", "actions": [{"type": "finish"}]},
                        "execution_results": [{"ok": True, "action": {"type": "finish"}}],
                        "verification": {"matched": True},
                    }
                ],
            },
            platform="windows",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "run.json"
            log_path.write_text(json.dumps({"final_status": "finished", "steps": [], "trace": trace}), encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                windows_interface.cmd_plan_log(type("Args", (), {"path": log_path, "output": None})())
            payload = json.loads(stdout.getvalue())

            self.assertEqual(payload["trace_summary"]["platform"], "windows")
            self.assertTrue(payload["trace_summary"]["verified"])

            old_log_path = Path(temp_dir) / "old.json"
            old_log_path.write_text(json.dumps({"final_status": "dry_run", "steps": []}), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                windows_interface.cmd_plan_log(type("Args", (), {"path": old_log_path, "output": None})())
            old_payload = json.loads(stdout.getvalue())

        self.assertIsNone(old_payload["trace_summary"])

    def test_fast_path_trace_and_replay_cli(self):
        payload = {
            "status": "success",
            "hwnd": 123,
            "chat": "张三",
            "steps": [
                {"step": "click_compose", "center": {"x": 10, "y": 20}, "result": {"ok": True, "mode": "coordinate"}},
                {"step": "verify_title_ocr", "verification": {"matched": True, "ocr_lines": ["张三"]}},
            ],
            "title_verification": {"matched": True},
        }

        trace = tactile_trace.build_fast_path_trace(payload, platform="windows", command="wechat-send-message")

        self.assertEqual(trace["task"]["source"], "fast_path")
        self.assertEqual(trace["target"]["chat_length"], 2)
        self.assertTrue(trace["outcome"]["verified"])
        self.assertGreaterEqual(trace["metrics"]["coordinate_action_count"], 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "run.json"
            log_path.write_text(json.dumps({"trace": trace}, ensure_ascii=False), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                windows_interface.cmd_trace_replay(type("Args", (), {"paths": [log_path], "output": None})())
            payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["trace_count"], 1)
        self.assertEqual(payload["by_platform"]["windows"]["trace_count"], 1)
        self.assertGreater(payload["verification_coverage"], 0)
        self.assertGreater(payload["coordinate_action_rate"], 0)
        self.assertEqual(payload["coordinate_sources"]["coordinate"], 1)
        self.assertEqual(payload["coordinate_source_known_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
