import tempfile
import unittest
from pathlib import Path

from uare.prune import PruneError, apply_prune, plan_prune


def _make(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "data.bin").write_bytes(b"x")


class PruneTests(unittest.TestCase):
    def test_removes_only_the_kept_family_and_keeps_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make(root, "extracted-assets", "extracted-assets-2022", "extracted-assets-game",
                  "ar-try1", "assetripper", "capture", "device-data", "native-decompile", "extracted",
                  "il2cpp-annotated-v2", "il2cpp-annotated", ".extracted-assets-dummy.abc.partial",
                  ".il2cpp-tables-v3.abc.partial", "blueprint")
            plan = plan_prune(root, ("extracted-assets-game",))
            self.assertEqual(plan.kept, ("extracted-assets-game",))
            self.assertEqual(set(plan.remove), {
                "extracted-assets", "extracted-assets-2022", ".extracted-assets-dummy.abc.partial",
            })
            removed = apply_prune(plan)
            self.assertEqual(set(removed), set(plan.remove))
            for kept in ("extracted-assets-game", "capture", "device-data", "native-decompile", "extracted",
                         "blueprint", "ar-try1", "assetripper", "il2cpp-annotated-v2", "il2cpp-annotated",
                         ".il2cpp-tables-v3.abc.partial"):
                self.assertTrue((root / kept).is_dir(), kept)
            for gone in plan.remove:
                self.assertFalse((root / gone).exists(), gone)

    def test_keeps_one_output_per_named_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make(root, "extracted-assets", "extracted-assets-dummy", "il2cpp-tables", "il2cpp-tables-v2",
                  "ar-try1", "assetripper-v2", "sprite-sheets", "sprite-sheets-v2")
            plan = plan_prune(root, ("extracted-assets-dummy", "il2cpp-tables-v2", "assetripper-v2"))
            self.assertEqual(set(plan.remove), {"extracted-assets", "il2cpp-tables", "ar-try1"})

    def test_keep_must_be_a_prunable_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make(root, "capture", "extracted-assets")
            with self.assertRaises(PruneError):
                plan_prune(root, ("capture",))

    def test_keep_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make(root, "extracted-assets")
            with self.assertRaises(PruneError):
                plan_prune(root, ("extracted-assets-game",))

    def test_keep_rejects_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make(root, "extracted-assets")
            with self.assertRaises(PruneError):
                plan_prune(root, ("../extracted-assets",))

    def test_dry_run_removes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make(root, "extracted-assets", "extracted-assets-game")
            plan_prune(root, ("extracted-assets-game",))
            self.assertTrue((root / "extracted-assets").is_dir())

    def test_prefix_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make(root, "extracted-assetsX", "extracted-assets-game")
            plan = plan_prune(root, ("extracted-assets-game",))
            self.assertEqual(plan.remove, ())


if __name__ == "__main__":
    unittest.main()
