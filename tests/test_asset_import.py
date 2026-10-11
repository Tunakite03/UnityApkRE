import tempfile
import unittest
from pathlib import Path

from uare.asset_import import generate_meta_tree, guid_for, meta_content


class GuidForTests(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(guid_for("Assets/Textures/hero.png"), guid_for("Assets/Textures/hero.png"))

    def test_different_paths_different_guids(self):
        self.assertNotEqual(guid_for("a.png"), guid_for("b.png"))

    def test_is_32_hex_chars(self):
        guid = guid_for("test")
        self.assertEqual(len(guid), 32)
        int(guid, 16)


class MetaContentTests(unittest.TestCase):
    def test_folder_meta(self):
        content = meta_content("Assets/Textures", is_dir=True)
        self.assertIn("folderAsset: yes", content)
        self.assertIn("fileFormatVersion: 2", content)

    def test_texture_meta(self):
        content = meta_content("Assets/hero.png", is_dir=False)
        self.assertIn("TextureImporter:", content)
        self.assertIn("textureType: 0", content)

    def test_sprite_meta(self):
        content = meta_content("Assets/sprites/icon.png", is_dir=False, parent_dir_name="sprites")
        self.assertIn("textureType: 8", content)

    def test_audio_meta(self):
        content = meta_content("Assets/bgm.wav", is_dir=False)
        self.assertIn("AudioImporter:", content)

    def test_script_meta(self):
        content = meta_content("Assets/Player.cs", is_dir=False)
        self.assertIn("MonoImporter:", content)

    def test_shader_meta(self):
        content = meta_content("Assets/test.shader", is_dir=False)
        self.assertIn("ShaderImporter:", content)

    def test_mesh_meta(self):
        content = meta_content("Assets/model.fbx", is_dir=False)
        self.assertIn("ModelImporter:", content)

    def test_unknown_ext_gets_default(self):
        content = meta_content("Assets/data.bin", is_dir=False)
        self.assertIn("DefaultImporter:", content)


class GenerateMetaTreeTests(unittest.TestCase):
    def test_generates_meta_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "Assets"
            root.mkdir()
            tex_dir = root / "Textures"
            tex_dir.mkdir()
            (tex_dir / "hero.png").write_bytes(b"\x89PNG")
            (root / "script.cs").write_text("class Foo {}")
            result = generate_meta_tree(root)
            self.assertEqual(result.files_processed, 2)
            self.assertEqual(result.folders_processed, 1)
            self.assertTrue((tex_dir / "hero.png.meta").is_file())
            self.assertTrue((root / "script.cs.meta").is_file())
            self.assertTrue((root / "Textures.meta").is_file())

    def test_separate_output_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "source"
            root.mkdir()
            (root / "test.wav").write_bytes(b"RIFF")
            out = Path(tmpdir) / "meta_out"
            out.mkdir()
            result = generate_meta_tree(root, out)
            self.assertEqual(result.meta_written, 1)
            self.assertTrue((out / "test.wav.meta").is_file())
            self.assertFalse((root / "test.wav.meta").exists())

    def test_skips_existing_meta_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "Assets"
            root.mkdir()
            (root / "file.txt").write_text("hello")
            (root / "file.txt.meta").write_text("existing")
            result = generate_meta_tree(root)
            self.assertEqual(result.files_processed, 1)
            self.assertNotEqual((root / "file.txt.meta").read_text(), "existing")


if __name__ == "__main__":
    unittest.main()
