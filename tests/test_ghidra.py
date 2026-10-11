import sys
import tempfile
import unittest
from pathlib import Path

from uare.errors import UareError
from uare.ghidra import (
    build_command,
    decompile,
    parse_functions,
    resolve_ghidra,
    write_script,
)

_FAKE_HEADLESS = """#!{python}
import os, sys
from pathlib import Path
assert not any(part.startswith(".") for part in Path(sys.argv[1]).parts)
i = sys.argv.index("-postScript")
out = sys.argv[i + 2]
os.makedirs(os.path.join(out, "decompiled"), exist_ok=True)
with open(os.path.join(out, "functions.tsv"), "w") as f:
    f.write("00100000\\tShopPage_Buy\\t120\\n")
    f.write("00100100\\tPlayer_Update\\t64\\n")
if "decompile" in sys.argv:
    with open(os.path.join(out, "decompiled", "0x100000_ShopPage_Buy.c"), "w") as f:
        f.write("void ShopPage_Buy(void){{}}\\n")
with open(os.path.join(out, "export-complete.txt"), "w") as f:
    f.write("complete\\n")
"""


def _write_exec(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


class GhidraTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_script_mentions_ghidra_api(self) -> None:
        path = write_script(self.tmp / "scripts")
        text = path.read_text()
        self.assertIn("getScriptArgs", text)
        self.assertIn("functions.tsv", text)

    def test_build_command_shape(self) -> None:
        argv = build_command("/opt/ghidra/analyzeHeadless", Path("/proj"), Path("/x.so"),
                             Path("/s/UareExport.java"), Path("/out"), decompile=True)
        self.assertIn("-import", argv)
        self.assertIn("/x.so", argv)
        self.assertIn("decompile", argv)
        self.assertEqual(argv[-1], "-deleteProject")

    def test_parse_functions(self) -> None:
        path = self.tmp / "functions.tsv"
        path.write_text("00100000\tA\t10\nnope\n00100010\tB\t20\n")
        funcs = parse_functions(path)
        self.assertEqual([(f.address, f.name, f.size) for f in funcs],
                         [(0x100000, "A", 10), (0x100010, "B", 20)])

    def test_resolve_ghidra_and_run(self) -> None:
        headless = _write_exec(self.tmp / "analyzeHeadless", _FAKE_HEADLESS.format(python=sys.executable))
        resolved = resolve_ghidra(str(headless), {}, self.tmp / "vendor", lambda name: None)
        so = self.tmp / "libil2cpp.so"
        so.write_bytes(b"\x7fELF" + b"\x00" * 64)
        out = self.tmp / "out"
        result = decompile(so, out, resolved, decompile_functions=True)
        self.assertEqual(result.functions, 2)
        self.assertEqual(result.decompiled, 1)
        self.assertTrue((out / "functions.tsv").is_file())
        self.assertEqual(len(list((out / "decompiled").glob("*.c"))), 1)

    def test_resolve_missing_is_an_error(self) -> None:
        with self.assertRaises(UareError):
            resolve_ghidra(None, {}, self.tmp / "vendor", lambda name: None)

    def test_zero_exit_without_completed_export_is_an_error(self) -> None:
        headless = _write_exec(self.tmp / "analyzeHeadless", "#!/bin/sh\nexit 0\n")
        so = self.tmp / "libil2cpp.so"
        so.write_bytes(b"\x7fELF")
        with self.assertRaisesRegex(UareError, "export did not complete"):
            decompile(so, self.tmp / "out", str(headless))
        self.assertFalse((self.tmp / "out").exists())


if __name__ == "__main__":
    unittest.main()
