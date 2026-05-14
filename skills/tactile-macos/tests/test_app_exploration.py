import importlib.util
import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "app_exploration.py"
SPEC = importlib.util.spec_from_file_location("app_exploration", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
app_exploration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app_exploration
SPEC.loader.exec_module(app_exploration)


class AppExplorationTests(unittest.TestCase):
    def make_temp_app(self, root: Path) -> Path:
        app_path = root / "Lark.app"
        contents = app_path / "Contents"
        resources = contents / "Resources"
        resources.mkdir(parents=True)
        (resources / "app.asar").write_text("", encoding="utf-8")
        (resources / "package.json").write_text('{"name":"lark"}', encoding="utf-8")
        locale = resources / "zh-Hans.lproj"
        locale.mkdir()
        (locale / "Localizable.strings").write_text('"Search" = "搜索";\n', encoding="utf-8")
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleName": "Lark",
                    "CFBundleDisplayName": "飞书",
                    "CFBundleIdentifier": "com.electron.lark",
                    "CFBundleShortVersionString": "7.0.0",
                    "CFBundleExecutable": "Lark",
                    "CFBundleURLTypes": [
                        {"CFBundleURLName": "Lark", "CFBundleURLSchemes": ["lark", "feishu"]}
                    ],
                },
                handle,
            )
        return app_path

    def test_profile_app_detects_electron_and_url_schemes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = self.make_temp_app(Path(temp_dir))

            profile = app_exploration.profile_target(str(app_path))

        self.assertEqual(profile["app_key"], "feishu")
        self.assertEqual(profile["group"], "electron-web")
        self.assertIn("electron", profile["detected_tech_stack"])
        self.assertEqual(profile["identity"]["bundle_id"], "com.electron.lark")
        self.assertEqual(profile["public_interfaces"]["url_schemes"], ["lark", "feishu"])
        hint_paths = [item["path"] for item in profile["bundle_probes"]["resource_hints"]]
        self.assertTrue(any(path.endswith("Resources/app.asar") for path in hint_paths))
        self.assertTrue(profile["bundle_probes"]["localization_samples"])

    def test_catalog_contains_domestic_app_tasks_and_verifiers(self):
        profile = app_exploration.synthetic_profile_for_known_app(app_exploration.KNOWN_APPS[0])

        catalog = app_exploration.catalog_from_profile(profile)
        action_ids = {action["id"] for action in catalog["actions"]}

        self.assertIn("feishu.open_messages", action_ids)
        self.assertIn("feishu.open_chat_draft", action_ids)
        self.assertEqual(catalog["app_guide_metadata"]["source"], "catalog_actions")
        self.assertIn("intents", catalog["app_guide_metadata"])
        self.assertTrue(
            any(intent["id"] == "feishu.open_messages" for intent in catalog["app_guide_metadata"]["intents"])
        )
        for action in catalog["actions"]:
            self.assertIsInstance(action["verifier"], dict)

    def test_router_chooses_fast_path_for_code_aware_and_ax_for_ax_strategy(self):
        catalog = app_exploration.catalog_for_app("feishu")
        action = app_exploration.find_action(catalog, "feishu.open_messages")

        code_route = app_exploration.route_action(action, "code-aware")
        ax_route = app_exploration.route_action(action, "ax")
        visual_route = app_exploration.route_action(action, "visual")

        self.assertEqual(code_route["selected_actuator"]["kind"], "fast_command")
        self.assertEqual(ax_route["selected_actuator"]["kind"], "ax_action")
        self.assertEqual(visual_route["selected_actuator"]["kind"], "visual")
        self.assertGreater(ax_route["fallback_count"], 0)

    def test_code_aware_respects_action_specific_preferred_actuator(self):
        catalog = app_exploration.catalog_for_app("wechat")
        action = app_exploration.find_action(catalog, "wechat.open_chat")

        route = app_exploration.route_action(action, "code-aware")

        self.assertEqual(route["selected_actuator"]["kind"], "ocr_coordinate")
        self.assertEqual(route["fallback_count"], 0)

    def test_run_adapter_returns_dry_run_trace_with_verifier(self):
        result = app_exploration.run_adapter(
            "wechat",
            "wechat.draft_message",
            strategy="ax",
            verify=True,
            inputs={"contact": "张三", "message": "收到"},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["verification"]["status"], "planned")
        self.assertEqual(result["steps"][0]["inputs"]["contact"], "张三")
        self.assertEqual(result["trace"]["kind"], "tactile_trace")
        self.assertEqual(result["trace"]["task"]["source"], "adapter_dry_run")
        self.assertEqual(result["trace"]["outcome"]["verification_status"], "planned")

    def test_eval_suite_simple_yaml_and_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            suite_path = Path(temp_dir) / "suite.yaml"
            suite_path.write_text(
                "name: smoke\n"
                "tasks:\n"
                "  - app: feishu\n"
                "    task: feishu.open_messages\n"
                "  - app: tencent-meeting\n"
                "    task: tencent-meeting.draft_topic\n",
                encoding="utf-8",
            )

            runs, summary = app_exploration.eval_suite(suite_path, strategy="code-aware", runs=2)

        self.assertEqual(len(runs), 4)
        self.assertTrue(all(run.get("trace", {}).get("task", {}).get("source") == "adapter_dry_run" for run in runs))
        self.assertEqual(summary["total_runs"], 4)
        self.assertEqual(summary["verification_coverage"], 1.0)
        self.assertIn("feishu.open_messages", summary["by_task"])

    def test_eval_suite_accepts_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            suite_path = Path(temp_dir) / "suite.json"
            suite_path.write_text(
                json.dumps({"name": "json-smoke", "tasks": [{"app": "wechat", "task": "wechat.open_chat"}]}),
                encoding="utf-8",
            )

            runs, summary = app_exploration.eval_suite(suite_path, strategy="visual", runs=1)

        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["success"])
        self.assertEqual(summary["task_success_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
