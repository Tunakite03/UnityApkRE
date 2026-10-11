import tempfile
import unittest
from pathlib import Path

from uare.mono_decompile import (
    DecompilerError,
    _is_game_assembly,
    resolve_decompiler,
)


class IsGameAssemblyTests(unittest.TestCase):
    def test_game_assembly(self):
        self.assertTrue(_is_game_assembly("Assembly-CSharp"))
        self.assertTrue(_is_game_assembly("GameCore"))

    def test_system_assembly(self):
        self.assertFalse(_is_game_assembly("mscorlib"))
        self.assertFalse(_is_game_assembly("System"))
        self.assertFalse(_is_game_assembly("System.Core"))

    def test_unity_assembly(self):
        self.assertFalse(_is_game_assembly("UnityEngine"))
        self.assertFalse(_is_game_assembly("UnityEngine.UI"))
        self.assertFalse(_is_game_assembly("Unity.Mathematics"))

    def test_system_prefix(self):
        self.assertFalse(_is_game_assembly("System.Collections"))
        self.assertFalse(_is_game_assembly("Microsoft.Win32"))

    def test_mono_prefix(self):
        self.assertFalse(_is_game_assembly("Mono.Data.Sqlite"))


class ResolveDecompilerTests(unittest.TestCase):
    def test_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exe = Path(tmpdir) / "ilspycmd"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            result = resolve_decompiler(str(exe), {}, Path("/vendor"), lambda n: None)
            self.assertEqual(result, str(exe))

    def test_explicit_missing_raises(self):
        with self.assertRaises(DecompilerError):
            resolve_decompiler("/no/such/file", {}, Path("/vendor"), lambda n: None)

    def test_env_var(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exe = Path(tmpdir) / "ilspycmd"
            exe.write_text("#!/bin/sh\n")
            result = resolve_decompiler(None, {"UARE_ILSPY": str(exe)}, Path("/vendor"), lambda n: None)
            self.assertEqual(result, str(exe))

    def test_path_fallback(self):
        result = resolve_decompiler(None, {}, Path("/no/vendor"),
                                    lambda n: "/usr/local/bin/ilspycmd" if n == "ilspycmd" else None)
        self.assertEqual(result, "/usr/local/bin/ilspycmd")

    def test_nothing_found_raises(self):
        with self.assertRaises(DecompilerError):
            resolve_decompiler(None, {}, Path("/no/vendor"), lambda n: None)


if __name__ == "__main__":
    unittest.main()
