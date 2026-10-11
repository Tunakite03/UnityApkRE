import tempfile
import unittest
from pathlib import Path

from uare.il2cpp_merge import merge, _escape_cs, _render_type, MergedType, MergedMethod, MergedField, MergedProperty, MergedEvent


class EscapeCsTests(unittest.TestCase):
    def test_simple_name(self):
        self.assertEqual(_escape_cs("Player"), "Player")

    def test_special_chars(self):
        result = _escape_cs("IEnumerable<int>")
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)

    def test_empty_name(self):
        self.assertEqual(_escape_cs(""), "_unnamed")

    def test_dots_preserved(self):
        self.assertEqual(_escape_cs("System.Object"), "System.Object")


class RenderTypeTests(unittest.TestCase):
    def test_basic_type_with_method(self):
        mt = MergedType(0, "Player", "Game", (
            MergedMethod(0, "Update", None, None, None),
        ), (), (), ())
        source = _render_type(mt)
        self.assertIn("namespace Game", source)
        self.assertIn("class Player", source)
        self.assertIn("void Update()", source)

    def test_method_with_rva(self):
        mt = MergedType(0, "Foo", "", (
            MergedMethod(0, "Bar", 0x12345, None, None),
        ), (), (), ())
        source = _render_type(mt)
        self.assertIn("RVA: 0x12345", source)

    def test_method_with_pseudo_c(self):
        mt = MergedType(0, "Foo", "", (
            MergedMethod(0, "Baz", 0xABC, 0xABC, "int x = 42;\nreturn x;"),
        ), (), (), ())
        source = _render_type(mt)
        self.assertIn("// int x = 42;", source)
        self.assertIn("// return x;", source)

    def test_fields_and_properties(self):
        mt = MergedType(0, "Data", "NS", (),
                        (MergedField(0, "health"),), (MergedProperty(0, "Name"),),
                        (MergedEvent(0, "OnDeath"),))
        source = _render_type(mt)
        self.assertIn("object health;", source)
        self.assertIn("object Name { get; set; }", source)
        self.assertIn("event System.Action OnDeath", source)


class MergeTests(unittest.TestCase):
    def test_merge_basic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp = Path(tmpdir) / "il2cpp"
            il2cpp.mkdir()
            (il2cpp / "types.tsv").write_text("0\tGame\tPlayer\n1\t\tGlobal\n")
            (il2cpp / "members.tsv").write_text(
                "0\tPlayer\tmethod\t0\tUpdate\n"
                "0\tPlayer\tfield\t0\thealth\n"
                "1\tGlobal\tmethod\t1\tInit\n"
            )
            out = Path(tmpdir) / "out"
            result = merge(il2cpp, None, None, out)
            self.assertEqual(result.types, 2)
            self.assertEqual(result.methods_total, 2)
            self.assertEqual(result.files_written, 2)
            self.assertTrue((out / "Game" / "Player.cs").is_file())
            self.assertTrue((out / "_global" / "Global.cs").is_file())

    def test_merge_with_rva(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp = Path(tmpdir) / "il2cpp"
            il2cpp.mkdir()
            (il2cpp / "types.tsv").write_text("0\tNS\tFoo\n")
            (il2cpp / "members.tsv").write_text("0\tFoo\tmethod\t0\tBar\n")
            enrich = Path(tmpdir) / "enrich"
            enrich.mkdir()
            (enrich / "methods_rva.tsv").write_text("0\t0x1234\n")
            out = Path(tmpdir) / "out"
            result = merge(il2cpp, enrich, None, out)
            self.assertEqual(result.methods_with_rva, 1)

    def test_merge_report_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp = Path(tmpdir) / "il2cpp"
            il2cpp.mkdir()
            (il2cpp / "types.tsv").write_text("0\t\tTest\n")
            (il2cpp / "members.tsv").write_text("")
            out = Path(tmpdir) / "out"
            merge(il2cpp, None, None, out)
            import json
            report = json.loads((out / "merge_report.json").read_text())
            self.assertEqual(report["types"], 1)


if __name__ == "__main__":
    unittest.main()
