import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "windows_interface.py"
ARTIFACTS_PATH = SKILL_ROOT / "scripts" / "utils" / "artifacts.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


artifacts = load_module("tactile_windows_artifacts", ARTIFACTS_PATH)
windows_interface = load_module("tactile_windows_interface", SCRIPT_PATH)


class TactileWindowsArtifactTests(unittest.TestCase):
    def test_artifact_dir_uses_windows_subdirectory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / "session-1"

            artifact_dir = artifacts.session_artifact_dir(env={"TACTILE_SESSION_DIR": str(session_dir)})

            self.assertEqual(artifact_dir, session_dir / "windows-app-workflow")
            self.assertTrue(artifact_dir.is_dir())

    def test_temp_output_path_is_relocated_to_session_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / "session-1"
            original_env = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update({"TACTILE_SESSION_DIR": str(session_dir)})

                output = windows_interface.session_scoped_output_path(Path(tempfile.gettempdir()) / "run.json")
            finally:
                os.environ.clear()
                os.environ.update(original_env)

            self.assertEqual(output, session_dir / "windows-app-workflow" / "run.json")

    def test_cli_artifact_dir_command_does_not_require_windows_sdk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / "session-1"
            env = dict(os.environ)
            env["TACTILE_SESSION_DIR"] = str(session_dir)

            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "artifact-dir"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=True,
            )

            self.assertEqual(Path(proc.stdout.strip()), session_dir / "windows-app-workflow")

    def test_repo_path_uses_bundled_windows_sdk_by_default(self):
        original_env = dict(os.environ)
        try:
            os.environ.pop("WINDOWS_USE_SDK_ROOT", None)

            repo = windows_interface.repo_path(None)
        finally:
            os.environ.clear()
            os.environ.update(original_env)

        self.assertEqual(repo, windows_interface.DEFAULT_REPO)
        self.assertTrue((repo / "WindowsUseSDK.ps1").exists())

    def test_workflow_appends_session_plan_output_for_execute(self):
        calls: list[tuple[list[str], Path]] = []

        class FakeCompleted:
            returncode = 0

        def fake_subprocess_run(cmd, *, cwd):
            calls.append((cmd, cwd))
            return FakeCompleted()

        with tempfile.TemporaryDirectory() as temp_dir:
            sdk_root = Path(temp_dir) / "WindowsUseSDK"
            workflow_dir = sdk_root / "workflows"
            workflow_dir.mkdir(parents=True)
            (sdk_root / "WindowsUseSDK.ps1").write_text("", encoding="utf-8")
            (workflow_dir / "llm_app_workflow.py").write_text("", encoding="utf-8")

            session_dir = Path(temp_dir) / "session-1"
            original_env = dict(os.environ)
            original_run = windows_interface.subprocess.run
            try:
                os.environ.clear()
                os.environ.update({"TACTILE_SESSION_DIR": str(session_dir)})
                windows_interface.subprocess.run = fake_subprocess_run
                code = windows_interface.cmd_workflow(
                    type(
                        "Args",
                        (),
                        {
                            "repo": str(sdk_root),
                            "instruction": "inspect Calculator",
                            "workflow_args": ["--", "--target", "Calculator", "--execute"],
                        },
                    )()
                )
            finally:
                windows_interface.subprocess.run = original_run
                os.environ.clear()
                os.environ.update(original_env)

            self.assertEqual(code, 0)
            cmd, cwd = calls[0]
            self.assertEqual(cwd.resolve(), sdk_root.resolve())
            self.assertIn("--plan-output", cmd)
            plan_path = Path(cmd[cmd.index("--plan-output") + 1])
            self.assertEqual(plan_path.parent, session_dir / "windows-app-workflow")

    def test_wechat_profile_regions_use_frame_relative_points(self):
        frame = {"x": 100.0, "y": 50.0, "width": 800.0, "height": 600.0}

        regions = windows_interface.wechat_profile_regions(frame)

        self.assertEqual(regions["search_center"], (225.0, 105.0))
        self.assertEqual(regions["compose_center"], (460.0, 565.0))
        self.assertEqual(regions["send_center"], (840.0, 608.0))
        self.assertEqual(regions["title_ocr"], (335.0, 85.0, 260, 40))
        self.assertEqual(regions["draft_ocr"], (320.0, 370.0, 550.0, 240))
        self.assertEqual(regions["recent_sent_ocr"], (335.0, 140.0, 525.0, 350))

    def test_parser_includes_wechat_send_message_and_clears_draft_by_default(self):
        parser = windows_interface.build_parser()

        args = parser.parse_args(["wechat-send-message", "--chat", "someone", "--message", "hello", "--draft-only"])

        self.assertEqual(args.chat, "someone")
        self.assertEqual(args.message, "hello")
        self.assertTrue(args.draft_only)
        self.assertEqual(args.send_method, "enter")
        self.assertFalse(args.keep_existing_draft)
        self.assertIs(args.func, windows_interface.cmd_wechat_send_message)

    def test_feishu_child_send_fallback_uses_compose_native_handle(self):
        calls: list[tuple[str, tuple]] = []

        def fake_click_point(repo, hwnd, x, y):
            calls.append(("click", (hwnd, x, y)))
            return {"status": "success"}

        def fake_run_sdk(repo, args, timeout):
            calls.append(("run_sdk", tuple(args)))
            return {
                "element": {
                    "role": "Pane",
                    "class_name": "Chrome_RenderWidgetHostHWND",
                    "native_window_handle": 43785422,
                }
            }

        def fake_keypress(repo, hwnd, key):
            calls.append(("keypress", (hwnd, key)))
            return {"status": "success"}

        def fake_verify(repo, hwnd, chat, message):
            calls.append(("verify", (hwnd, chat, message)))
            return {"confirmed": True, "compose_cleared": True}

        original_click_point = windows_interface.click_point
        original_run_sdk = windows_interface.run_sdk
        original_keypress = windows_interface.keypress
        original_verify = windows_interface.verify_feishu_message_sent
        original_sleep = windows_interface.time.sleep
        try:
            windows_interface.click_point = fake_click_point
            windows_interface.run_sdk = fake_run_sdk
            windows_interface.keypress = fake_keypress
            windows_interface.verify_feishu_message_sent = fake_verify
            windows_interface.time.sleep = lambda seconds: None

            result = windows_interface.send_feishu_via_compose_child(
                Path("repo"),
                921816,
                chat="张三",
                message="天气",
                ready={"compose_center": [966.83, 1459.75]},
                wait_ms=0,
            )
        finally:
            windows_interface.click_point = original_click_point
            windows_interface.run_sdk = original_run_sdk
            windows_interface.keypress = original_keypress
            windows_interface.verify_feishu_message_sent = original_verify
            windows_interface.time.sleep = original_sleep

        self.assertTrue(result["attempted"])
        self.assertEqual(result["child_hwnd"], 43785422)
        self.assertIn(("keypress", (43785422, "enter")), calls)
        self.assertEqual(result["post_send_verification"], {"confirmed": True, "compose_cleared": True})

    def test_feishu_search_rows_prefer_contact_title_over_group_snippets(self):
        target = "\u5f20\u4e09"
        payload = {
            "capture": {"region": {"x": 0, "y": 0, "width": 1800, "height": 1200}},
            "lines": [
                {"text": target, "screen_frame": {"x": 80, "y": 120, "width": 90, "height": 28}},
                {"text": "\u793a\u4f8b\u90e8\u95e8", "screen_frame": {"x": 80, "y": 158, "width": 180, "height": 24}},
                {"text": "\u793a\u4f8b\u7fa4\u804a(3) \u5916\u90e8", "screen_frame": {"x": 80, "y": 250, "width": 260, "height": 28}},
                {"text": "\u5305\u542b\uff1a\u5f20\u4e09 | \u7fa4\u6d88\u606f\u66f4\u65b0\u4e8e 2025\u5e7412\u67088\u65e5", "screen_frame": {"x": 80, "y": 288, "width": 520, "height": 24}},
                {"text": "\u5f20\u4e09 \u7fa4\u6d88\u606f\u66f4\u65b0\u4e8e 2025\u5e7412\u67088\u65e5", "screen_frame": {"x": 80, "y": 350, "width": 520, "height": 24}},
            ],
        }

        accepted, rejected = windows_interface.find_feishu_contact_result_lines(payload, target)

        self.assertEqual([line["text"] for line in accepted], [target])
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(line.get("_reject_reason") for line in rejected))

    def test_feishu_open_chat_refuses_rejected_search_rows_without_clicking_first_result(self):
        target = "\u5f20\u4e09"
        payload = {
            "capture": {"region": {"x": 0, "y": 0, "width": 1800, "height": 1200}},
            "text": "\u5305\u542b\uff1a\u5f20\u4e09 | \u7fa4\u6d88\u606f\u66f4\u65b0\u4e8e 2025\u5e7412\u67088\u65e5",
            "lines": [
                {"text": "\u5305\u542b\uff1a\u5f20\u4e09 | \u7fa4\u6d88\u606f\u66f4\u65b0\u4e8e 2025\u5e7412\u67088\u65e5", "screen_frame": {"x": 80, "y": 288, "width": 520, "height": 24}},
            ],
        }
        calls: list[str] = []

        original_resolve = windows_interface.resolve_target_hwnd
        original_keypress = windows_interface.keypress
        original_input_action = windows_interface.input_action
        original_ocr_window = windows_interface.ocr_window
        original_elements = windows_interface.elements_for_window
        original_click_element_center = windows_interface.click_element_center
        original_sleep = windows_interface.time.sleep
        try:
            windows_interface.resolve_target_hwnd = lambda repo, target_name, hwnd: (123, {"mode": "test"})
            windows_interface.keypress = lambda repo, hwnd, key: {"ok": True}
            windows_interface.input_action = lambda repo, hwnd, action, text: {"status": "success"}
            windows_interface.ocr_window = lambda repo, hwnd: payload
            windows_interface.elements_for_window = lambda repo, hwnd, query=None, view="control", limit=80: {
                "elements": [
                    {
                        "role": "VirtualRegion",
                        "text": "Feishu/Lark first search/chat result candidate",
                        "center": [100, 100],
                    }
                ]
            }

            def fake_click_element_center(repo, hwnd, element):
                calls.append("clicked")
                return {"status": "success"}

            windows_interface.click_element_center = fake_click_element_center
            windows_interface.time.sleep = lambda seconds: None

            code, result = windows_interface.perform_feishu_open_chat(
                Path("repo"),
                target="Feishu",
                hwnd=123,
                chat=target,
                wait_ms=0,
                verify_result=True,
                allow_first_result=True,
                dry_run=False,
            )
        finally:
            windows_interface.resolve_target_hwnd = original_resolve
            windows_interface.keypress = original_keypress
            windows_interface.input_action = original_input_action
            windows_interface.ocr_window = original_ocr_window
            windows_interface.elements_for_window = original_elements
            windows_interface.click_element_center = original_click_element_center
            windows_interface.time.sleep = original_sleep

        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(calls, [])
        self.assertIn("non-contact rows", result["reason"])


if __name__ == "__main__":
    unittest.main()
