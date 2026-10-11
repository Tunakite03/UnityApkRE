import json
import tempfile
import unittest
from pathlib import Path

from uare.rebuild import rebuild, _detect_unity_version, _copy_tree
from uare.game_model import GameModel, save_model


def _setup_workdir(root: Path) -> None:
    """Create a minimal workdir structure that rebuild expects."""
    analysis = root / "analysis"
    analysis.mkdir(parents=True)
    (analysis / "report.json").write_text(json.dumps({
        "scripting_backend": "il2cpp",
        "inventory": {
            "unity_versions": {"2022.3.10f1": 5, "2021.3.0f1": 2},
            "target_platforms": {"Android": 7},
            "scene_files": ["level0"],
        },
        "il2cpp": {"status": "ok", "version": 31},
        "native_libraries": [],
    }))
    assets = root / "extracted-assets"
    tex = assets / "shared" / "textures"
    tex.mkdir(parents=True)
    (tex / "hero.png").write_bytes(b"\x89PNG")
    audio = assets / "shared" / "audio"
    audio.mkdir(parents=True)
    (audio / "bgm.wav").write_bytes(b"RIFF")
    scripts = assets / "shared" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "Game.cs").write_text("class Game {}")
    save_model(assets / "game_model.json", GameModel((), (), (), ()))

    extracted = root / "extracted"
    extracted.mkdir(parents=True)


class DetectUnityVersionTests(unittest.TestCase):
    def test_picks_most_common_version(self):
        report = {"inventory": {"unity_versions": {"2022.3.10f1": 5, "2021.3.0f1": 2}}}
        self.assertEqual(_detect_unity_version(report), "2022.3.10f1")

    def test_missing_version_requires_evidence(self):
        report = {"inventory": {"unity_versions": {}}}
        from uare.game_model import ModelError
        with self.assertRaises(ModelError):
            _detect_unity_version(report)


class CopyTreeTests(unittest.TestCase):
    def test_copies_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "a.txt").write_text("hello")
            sub = src / "sub"
            sub.mkdir()
            (sub / "b.txt").write_text("world")
            dst = Path(tmpdir) / "dst"
            count = _copy_tree(src, dst)
            self.assertEqual(count, 2)
            self.assertTrue((dst / "a.txt").is_file())
            self.assertTrue((dst / "sub" / "b.txt").is_file())

    def test_nonexistent_src_returns_zero(self):
        self.assertEqual(_copy_tree(Path("/nonexistent"), Path("/dst")), 0)


class RebuildTests(unittest.TestCase):
    def test_creates_project_structure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "work"
            workdir.mkdir()
            _setup_workdir(workdir)
            out = Path(tmpdir) / "project"
            result = rebuild(workdir, out, workdir / "extracted-assets")
            self.assertEqual(result.unity_version, "2022.3.10f1")
            self.assertEqual(result.scripting_backend, "il2cpp")
            self.assertTrue((out / "ProjectSettings" / "ProjectVersion.txt").is_file())
            self.assertTrue((out / "Packages" / "manifest.json").is_file())
            version_txt = (out / "ProjectSettings" / "ProjectVersion.txt").read_text()
            self.assertIn("2022.3.10f1", version_txt)

    def test_copies_assets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "work"
            workdir.mkdir()
            _setup_workdir(workdir)
            out = Path(tmpdir) / "project"
            result = rebuild(workdir, out, workdir / "extracted-assets")
            self.assertGreater(result.textures, 0)
            self.assertGreater(result.audio, 0)
            self.assertTrue((out / "Assets" / "Textures" / "hero.png").is_file())

    def test_writes_rebuild_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "work"
            workdir.mkdir()
            _setup_workdir(workdir)
            out = Path(tmpdir) / "project"
            rebuild(workdir, out, workdir / "extracted-assets")
            report = json.loads((out / "rebuild_report.json").read_text())
            self.assertIn("unity_version", report)
            self.assertIn("steps", report)

    def test_missing_report_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "work"
            workdir.mkdir()
            from uare.errors import UareError
            with self.assertRaises(UareError):
                rebuild(workdir, Path(tmpdir) / "out", workdir / "extracted-assets")

    def test_legacy_assets_layout_is_refused_with_a_repackage_instruction(self):
        from uare.game_model import ModelError
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "work"
            _setup_workdir(workdir)
            assets = workdir / "extracted-assets"
            (assets / "shared").rename(assets / "assets")
            out = Path(tmpdir) / "out"
            with self.assertRaisesRegex(ModelError, "shared/ layout"):
                rebuild(workdir, out, assets)
            self.assertFalse(out.exists())

    def test_meta_generation_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir) / "work"
            workdir.mkdir()
            _setup_workdir(workdir)
            out = Path(tmpdir) / "project"
            result = rebuild(workdir, out, workdir / "extracted-assets")
            self.assertIn("meta_generation", result.steps_run)
            meta_files = list((out / "Assets").rglob("*.meta"))
            self.assertGreater(len(meta_files), 0)


if __name__ == "__main__":
    unittest.main()
