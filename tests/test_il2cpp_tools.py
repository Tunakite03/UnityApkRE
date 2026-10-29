import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from uare.errors import UareError
from uare.il2cpp_tools import (
    ResolvedDumper,
    enrich,
    parse_script_json,
    resolve_dumper,
)

_FAKE_DUMPER = """#!{python}
import json, os, sys
out = sys.argv[3]
os.makedirs(out, exist_ok=True)
json.dump({{"ScriptMethod": [
    {{"Address": 4096, "Name": "Game.UI.ShopPage$$Buy", "Signature": "void Buy(int id)"}},
    {{"Address": 8192, "Name": "Game.Player$$Update", "Signature": "void Update()"}},
]}}, open(os.path.join(out, "script.json"), "w"))
open(os.path.join(out, "dump.cs"), "w").write("// Il2CppDumper dump")
"""


def _write_exec(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


class ParseScriptTests(unittest.TestCase):
    def test_parses_methods(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.json"
            path.write_text(json.dumps({"ScriptMethod": [
                {"Address": 0x1000, "Name": "Game.UI.ShopPage$$Buy", "Signature": "void Buy(int)"},
                {"Address": "bad", "Name": "x"},
                {"NoAddress": 1},
            ]}))
            methods = parse_script_json(path)
        self.assertEqual(len(methods), 1)
        self.assertEqual((methods[0].address, methods[0].class_name, methods[0].method),
                         (0x1000, "Game.UI.ShopPage", "Buy"))

    def test_missing_list_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.json"
            path.write_text("{}")
            with self.assertRaises(UareError):
                parse_script_json(path)


class ResolveDumperTests(unittest.TestCase):
    def test_explicit_must_be_executable(self) -> None:
        with self.assertRaises(UareError):
            resolve_dumper("/nope/Il2CppDumper", {}, Path("/vendor"), lambda name: None)

    def test_vendored_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_exec(root / "Il2CppDumper", "#!/bin/sh\n")
            resolved = resolve_dumper(None, {}, root, lambda name: None)
        self.assertEqual(resolved.source, "vendored")


class EnrichTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        import sys
        self.dumper = _write_exec(self.tmp / "Il2CppDumper", _FAKE_DUMPER.format(python=sys.executable))
        libdir = self.tmp / "lib/arm64-v8a"
        libdir.mkdir(parents=True)
        (libdir / "libil2cpp.so").write_bytes(b"\x7fELF" + b"\x00" * 64)
        meta = self.tmp / "Data/Managed/Metadata"
        meta.mkdir(parents=True)
        (self.tmp / "Data/globalgamemanagers").write_bytes(b"gg")
        (meta / "global-metadata.dat").write_bytes(b"\xaf\x1b\xb1\xfa" + b"\x00" * 8)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_enrich_runs_dumper_and_parses_output(self) -> None:
        out = self.tmp / "out"
        result = enrich(self.tmp, out, ResolvedDumper(str(self.dumper), "--dumper"))
        self.assertEqual(result.methods, 2)
        self.assertTrue(result.dump_cs)
        rows = (out / "methods_rva.tsv").read_text().splitlines()
        self.assertEqual(rows[0], "0x1000\tGame.UI.ShopPage\tBuy\tvoid Buy(int id)")
        self.assertEqual((out / "dump.cs").read_text(), "// Il2CppDumper dump")
        report = json.loads((out / "enrich_report.json").read_text())
        self.assertEqual(report["methods"], 2)

    def test_dumper_that_exits_0_without_script_json_fails_and_leaves_no_output(self) -> None:
        import sys
        silent = _write_exec(self.tmp / "SilentDumper",
                             f"#!{sys.executable}\nprint('ERROR: Metadata file supplied is not valid metadata file.')\n")
        out = self.tmp / "out"
        with self.assertRaisesRegex(UareError, "no script.json.*not valid metadata"):
            enrich(self.tmp, out, ResolvedDumper(str(silent), "--dumper"))
        self.assertFalse(out.exists())

    def test_enrich_without_metadata_fails_loud(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        (empty / "Data").mkdir()
        (empty / "Data/globalgamemanagers").write_bytes(b"gg")
        with self.assertRaises(UareError):
            enrich(empty, self.tmp / "o2", ResolvedDumper(str(self.dumper), "--dumper"))


if __name__ == "__main__":
    unittest.main()
