import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from uare import cli
from uare.plan import Prerequisites, build_plan, summarize, workdir_state

READY = Prerequisites(adb="/opt/adb", adb_source="vendored", adb_error=None,
                      missing_modules=(), workdir_state="absent")


class BuildPlanTests(unittest.TestCase):
    def test_ready_plan_lists_four_steps_in_order(self) -> None:
        plan = build_plan("com.example.game", Path("/work/run1"), READY)
        self.assertTrue(plan.ready)
        self.assertEqual(plan.blockers, ())
        self.assertEqual([s.name for s in plan.steps], ["pull", "extract", "analyze", "package"])

    def test_commands_use_the_given_workdir(self) -> None:
        plan = build_plan("com.example.game", Path("/work/run1"), READY)
        self.assertEqual(plan.steps[0].command, "scripts/uare pull com.example.game --out /work/run1/pull")
        self.assertEqual(plan.steps[3].command,
                         "scripts/uare package /work/run1/extracted --out /work/run1/extracted-assets")

    def test_only_pull_needs_a_device_and_only_package_needs_unitypy(self) -> None:
        plan = build_plan("com.example.game", Path("/work/run1"), READY)
        self.assertEqual([s.needs_device for s in plan.steps], [True, False, False, False])
        self.assertEqual([s.needs_unitypy for s in plan.steps], [False, False, False, True])

    def test_missing_unitypy_blocks_the_plan(self) -> None:
        prereq = Prerequisites(adb="/opt/adb", adb_source="PATH", adb_error=None,
                               missing_modules=("UnityPy",), workdir_state="absent")
        plan = build_plan("com.example.game", Path("/work/run1"), prereq)
        self.assertFalse(plan.ready)
        self.assertIn("UnityPy", plan.blockers[0])

    def test_missing_adb_blocks_the_plan(self) -> None:
        prereq = Prerequisites(adb=None, adb_source=None, adb_error="adb not found",
                               missing_modules=(), workdir_state="absent")
        plan = build_plan("com.example.game", Path("/work/run1"), prereq)
        self.assertFalse(plan.ready)
        self.assertEqual(plan.blockers, ("adb: adb not found",))

    def test_populated_workdir_blocks_the_plan(self) -> None:
        prereq = Prerequisites(adb="/opt/adb", adb_source="vendored", adb_error=None,
                               missing_modules=(), workdir_state="populated")
        plan = build_plan("com.example.game", Path("/work/run1"), prereq)
        self.assertFalse(plan.ready)
        self.assertIn("not empty", plan.blockers[0])

    def test_summary_names_every_step_and_check(self) -> None:
        text = "\n".join(summarize(build_plan("com.example.game", Path("/work/run1"), READY)))
        for name in ("pull", "extract", "analyze", "package"):
            self.assertIn(f"{name}", text)
        self.assertIn("scripts/run-tests", text)


class WorkdirStateTests(unittest.TestCase):
    def test_absent_empty_and_populated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(workdir_state(root / "missing"), "absent")
            self.assertEqual(workdir_state(root), "empty")
            (root / "file").write_text("x", encoding="utf-8")
            self.assertEqual(workdir_state(root), "populated")


class PlanCommandTests(unittest.TestCase):
    def test_json_output_does_not_create_the_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "run1"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["plan", "com.example.game", "--workdir", str(workdir), "--json"])
            self.assertEqual(code, 0)
            self.assertFalse(workdir.exists())
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["package"], "com.example.game")
            self.assertEqual([s["name"] for s in payload["steps"]], ["pull", "extract", "analyze", "package"])


if __name__ == "__main__":
    unittest.main()
