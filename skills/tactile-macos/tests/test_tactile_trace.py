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
INTERFACE_PATH = SKILL_ROOT / "scripts" / "macos_interface.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tactile_trace = load_module("tactile_macos_trace", TRACE_PATH)
macos_interface = load_module("tactile_macos_interface_for_trace_tests", INTERFACE_PATH)


class MacosTactileTraceTests(unittest.TestCase):
    def test_trace_schema_metrics_and_post_input_verification(self):
        run_log = {
            "target": {"identifier": "/Applications/Lark.app", "pid": 31334},
            "instruction": "draft a message",
            "final_status": "finished",
            "steps": [
                {
                    "step": 1,
                    "target": {"app": "Lark", "pid": 31334},
                    "observation_sources": {
                        "ax_elements": 3,
                        "ocr_lines": 1,
                        "profile_regions": 0,
                        "screenshot_path": "/tmp/step.png",
                        "visual_observation": {"enabled": False, "image_attached_to_planner": False},
                    },
                    "plan": {
                        "status": "continue",
                        "summary": "type the draft",
                        "actions": [{"type": "writetext", "element_id": "e1", "text": "secret message"}],
                    },
                    "action_elements": [
                        {"element_id": "e1", "source": "ax", "direct_ax": True, "center": {"x": 20, "y": 30}}
                    ],
                    "execution_results": [
                        {
                            "ok": True,
                            "mode": "paste",
                            "action": {"type": "writetext", "element_id": "e1", "text_length": 14},
                            "input_diagnostics": {
                                "post_input_verification": {
                                    "expected_text_length": 14,
                                    "expected_text_visible": False,
                                    "observation_text_count": 5,
                                }
                            },
                        }
                    ],
                },
                {
                    "step": 2,
                    "target": {"app": "Lark", "pid": 31334},
                    "observation_sources": {"ax_elements": 2, "ocr_lines": 0, "profile_regions": 0},
                    "plan": {
                        "status": "continue",
                        "summary": "click visual fallback",
                        "actions": [{"type": "click", "x": 10, "y": 20, "source": "visual"}],
                    },
                    "action_elements": [
                        {"element_id": None, "source": "visual", "center": {"x": 10, "y": 20}}
                    ],
                    "execution_results": [
                        {
                            "ok": True,
                            "mode": "coordinate",
                            "action": {"type": "click", "x": 10, "y": 20, "source": "visual"},
                            "point": {"x": 10, "y": 20},
                            "fallback_from": "direct_ax",
                            "fallback_reason": "direct_ax_no_observation_change",
                        }
                    ],
                },
            ],
        }

        trace = tactile_trace.build_trace(run_log, platform="macos")

        self.assertEqual(trace["schema_version"], 1)
        self.assertEqual(trace["kind"], "tactile_trace")
        self.assertEqual(trace["platform"], "macos")
        self.assertEqual(trace["steps"][0]["plan"]["actions"][0]["text_length"], 14)
        self.assertNotIn("text", trace["steps"][0]["plan"]["actions"][0])
        self.assertNotIn("secret message", json.dumps(trace, ensure_ascii=False))
        self.assertEqual(trace["steps"][0]["verifications"][0]["status"], "failed")
        self.assertFalse(trace["outcome"]["verified"])
        self.assertEqual(trace["metrics"]["action_count"], 2)
        self.assertEqual(trace["metrics"]["ax_action_count"], 1)
        self.assertEqual(trace["metrics"]["visual_action_count"], 1)
        self.assertEqual(trace["metrics"]["coordinate_action_count"], 1)
        self.assertEqual(trace["metrics"]["fallback_count"], 1)
        self.assertEqual(trace["metrics"]["failed_verification_count"], 1)

    def test_plan_log_outputs_trace_summary_and_supports_old_logs(self):
        trace = tactile_trace.build_trace(
            {
                "target": {"identifier": "Calculator", "pid": 42},
                "instruction": "inspect",
                "final_status": "finished",
                "steps": [
                    {
                        "step": 1,
                        "plan": {"summary": "done", "actions": [{"type": "finish"}]},
                        "execution_results": [{"ok": True, "action": {"type": "finish"}}],
                        "post_input_verification": {"expected_text_visible": True},
                    }
                ],
            },
            platform="macos",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "run.json"
            log_path.write_text(json.dumps({"final_status": "finished", "steps": [], "trace": trace}), encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                macos_interface.cmd_plan_log(type("Args", (), {"path": log_path, "output": None})())
            payload = json.loads(stdout.getvalue())

            self.assertEqual(payload["trace_summary"]["platform"], "macos")
            self.assertTrue(payload["trace_summary"]["verified"])

            old_log_path = Path(temp_dir) / "old.json"
            old_log_path.write_text(json.dumps({"final_status": "dry_run", "steps": []}), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                macos_interface.cmd_plan_log(type("Args", (), {"path": old_log_path, "output": None})())
            old_payload = json.loads(stdout.getvalue())

        self.assertIsNone(old_payload["trace_summary"])

    def test_fast_path_trace_sanitizes_text_and_replay_summarizes_fixtures(self):
        payload = {
            "status": "success",
            "pid": 31334,
            "chat": "张三",
            "steps": [
                {"step": "input_search_text", "text": "张三", "method": "pastetext", "result": {"ok": True, "mode": "paste"}},
                {"step": "click_result", "center": {"x": 10, "y": 20}, "result": {"ok": True, "mode": "coordinate"}},
                {"step": "verify_chat", "verification": {"confirmed": True, "header_match": True}},
            ],
            "verification": {"confirmed": True},
        }

        trace = tactile_trace.build_fast_path_trace(payload, platform="macos", command="feishu-send-message")

        self.assertEqual(trace["task"]["source"], "fast_path")
        self.assertEqual(trace["target"]["chat_length"], 2)
        self.assertTrue(trace["outcome"]["verified"])
        self.assertNotIn("张三", json.dumps(trace, ensure_ascii=False))
        self.assertGreaterEqual(trace["metrics"]["passed_verification_count"], 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "run.json"
            log_path.write_text(json.dumps({"trace": trace}, ensure_ascii=False), encoding="utf-8")
            jsonl_path = Path(temp_dir) / "runs.jsonl"
            jsonl_path.write_text(json.dumps({"trace": trace}, ensure_ascii=False) + "\n", encoding="utf-8")

            replay = tactile_trace.replay_trace_files([log_path, jsonl_path])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                macos_interface.cmd_trace_replay(type("Args", (), {"paths": [log_path], "output": None})())
            cli_payload = json.loads(stdout.getvalue())

        self.assertEqual(replay["trace_count"], 2)
        self.assertEqual(replay["by_source"]["fast_path"]["trace_count"], 2)
        self.assertGreater(replay["verification_coverage"], 0)
        self.assertGreater(replay["coordinate_action_rate"], 0)
        self.assertEqual(replay["coordinate_sources"]["coordinate"], 2)
        self.assertEqual(replay["coordinate_source_known_rate"], 1.0)
        self.assertEqual(cli_payload["trace_count"], 1)


if __name__ == "__main__":
    unittest.main()
