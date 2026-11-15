import json
import tempfile
import unittest
from pathlib import Path

from uare.unity_import import UnityImportError, md4, script_file_id, unity_import

STUB_GUID = "a" * 32
SCRIPT_GUID = "b" * 32


class ScriptFileIdTests(unittest.TestCase):
    def test_md4_matches_rfc_1320(self) -> None:
        self.assertEqual(md4(b"").hex(), "31d6cfe0d16ae931b73c59d7e0c089c0")
        self.assertEqual(md4(b"abc").hex(), "a448017aaf21d8525fc10ae87aa6729d")

    def test_unity_ui_image_file_id(self) -> None:
        # The file ID every Unity project uses for UnityEngine.UI.Image inside UnityEngine.UI.dll.
        self.assertEqual(script_file_id("UnityEngine.UI", "Image"), -765806418)


class UnityImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.source = root / "remap" / "project"
        assets = self.source / "Assets"
        (assets / "Plugins").mkdir(parents=True)
        (assets / "Plugins" / "UnityEngine.UI.dll").write_bytes(b"MZ\0stub")
        (assets / "Plugins" / "UnityEngine.UI.dll.meta").write_text(f"guid: {STUB_GUID}\n")
        image = script_file_id("UnityEngine.UI", "Image")
        (assets / "Panel.prefab").write_text(f"  m_Script: {{fileID: {image}, guid: {STUB_GUID}, type: 3}}\n"
                                             f"  m_Other: {{fileID: 123, guid: {STUB_GUID}, type: 3}}\n")
        self.package = root / "ugui"
        (self.package / "Runtime").mkdir(parents=True)
        (self.package / "Runtime" / "UnityEngine.UI.asmdef").write_text(json.dumps({"name": "UnityEngine.UI"}))
        (self.package / "Runtime" / "Image.cs").write_text("namespace UnityEngine.UI\n{\n    public class Image : MaskableGraphic {}\n}\n")
        (self.package / "Runtime" / "Image.cs.meta").write_text(f"guid: {SCRIPT_GUID}\n")
        self.unity = root / "Game"
        (self.unity / "ProjectSettings").mkdir(parents=True)
        (self.unity / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.62f2\n")
        (self.unity / "Assets").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stub_assembly_is_replaced_by_the_package_script(self) -> None:
        result = unity_import(self.source, self.unity, [self.package])
        prefab = (self.unity / "Assets" / "Panel.prefab").read_text()
        self.assertIn(f"{{fileID: 11500000, guid: {SCRIPT_GUID}, type: 3}}", prefab)
        self.assertIn(f"{{fileID: 123, guid: {STUB_GUID}, type: 3}}", prefab, "an unknown type keeps its stub reference")
        self.assertFalse((self.unity / "Assets" / "Plugins" / "UnityEngine.UI.dll").exists())
        self.assertEqual((result.stub_assemblies_replaced, result.references_rebound), (("UnityEngine.UI",), 1))
        self.assertEqual(result.references_unresolved, {f"{STUB_GUID}:123": 1})
        self.assertTrue(Path(result.report).is_file())

    def test_stubs_are_repaired_and_package_dlls_replace_same_named_stubs(self) -> None:
        plugins = self.source / "Assets" / "Plugins"
        (plugins / "Game.dll").write_bytes(b"MZ\0<Module>\0UnitySourceGeneratedAssemblyMonoScriptTypes_v1\0Get\0")
        (plugins / "Game.dll.meta").write_text("guid: " + "c" * 32 + "\nPluginImporter:\n  isExplicitlyReferenced: 0\n"
                                               "  platformData:\n  - first:\n      Editor: Editor\n    second:\n      enabled: 0\n")
        scripts = self.source / "Assets" / "Scripts" / "Assembly-CSharp"
        scripts.mkdir(parents=True)
        (scripts / "Recovered.cs").write_text("public class Recovered : UnityEngine.MonoBehaviour {}\n")
        (plugins / "Newtonsoft.Json.dll").write_bytes(b"MZ\0stub")
        (plugins / "Newtonsoft.Json.dll.meta").write_text("guid: " + "d" * 32 + "\n")
        (self.source / "Assets" / "Data.asset").write_text("  m_Script: {fileID: 42, guid: " + "d" * 32 + ", type: 3}\n")
        json_package = self.package.parent / "newtonsoft"
        (json_package / "Runtime" / "AOT").mkdir(parents=True)
        for folder, guid in ((json_package / "Runtime", "e" * 32), (json_package / "Runtime" / "AOT", "f" * 32)):
            (folder / "Newtonsoft.Json.dll").write_bytes(b"MZ\0real")
            (folder / "Newtonsoft.Json.dll.meta").write_text(f"guid: {guid}\n")
        result = unity_import(self.source, self.unity, [self.package, json_package])
        game = (self.unity / "Assets" / "Plugins" / "Game.dll").read_bytes()
        self.assertNotIn(b"UnitySourceGeneratedAssemblyMonoScriptTypes_v1", game)
        self.assertEqual(len(game), len((plugins / "Game.dll").read_bytes()), "string heap offsets must not move")
        self.assertFalse((self.unity / "Assets" / "Plugins" / "Newtonsoft.Json.dll").exists())
        meta = (self.unity / "Assets" / "Plugins" / "Game.dll.meta").read_text()
        self.assertIn("isExplicitlyReferenced: 1", meta)
        self.assertIn("Editor: Editor\n    second:\n      enabled: 1", meta)
        definition = json.loads((self.unity / "Assets" / "Scripts" / "Assembly-CSharp" / "Assembly-CSharp-Recovered.asmdef").read_text())
        self.assertEqual(definition["precompiledReferences"], ["Game.dll", "Newtonsoft.Json.dll"])
        self.assertEqual(definition["references"], ["UnityEngine.UI"])
        self.assertIn("{fileID: 42, guid: " + "e" * 32 + ", type: 3}", (self.unity / "Assets" / "Data.asset").read_text(),
                      "the non-AOT package DLL wins and the file ID is kept")
        self.assertEqual((result.stub_dlls_replaced, result.stubs_repaired), (("Newtonsoft.Json",), 1))

    def test_a_differing_target_file_stops_the_import_before_any_write(self) -> None:
        (self.unity / "Assets" / "Panel.prefab").write_text("someone else's prefab\n")
        with self.assertRaises(UnityImportError):
            unity_import(self.source, self.unity, [self.package])
        self.assertFalse((self.unity / "Assets" / "Plugins").exists())

    def test_rerun_skips_identical_files(self) -> None:
        unity_import(self.source, self.unity, [self.package])
        again = unity_import(self.source, self.unity, [self.package])
        self.assertEqual((again.files_copied, again.files_identical), (0, 1))


if __name__ == "__main__":
    unittest.main()
