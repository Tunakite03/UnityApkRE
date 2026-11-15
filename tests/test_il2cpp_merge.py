import tempfile
import unittest
from pathlib import Path

from uare.errors import UareError
from uare.il2cpp_merge import merge, _escape_cs, _escape_namespace, _render_type, MergedType, MergedMethod, MergedField, MergedProperty, MergedEvent


class EscapeCsTests(unittest.TestCase):
    def test_simple_name(self):
        self.assertEqual(_escape_cs("Player"), "Player")

    def test_special_chars(self):
        result = _escape_cs("IEnumerable<int>")
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)

    def test_empty_name(self):
        self.assertEqual(_escape_cs(""), "_unnamed")

    def test_dots_replaced(self):
        self.assertEqual(_escape_cs("System.Object"), "System_Object")

    def test_keyword_is_verbatim(self):
        self.assertEqual(_escape_cs("try"), "@try")

    def test_leading_digit_prefixed(self):
        self.assertEqual(_escape_cs("8005ABC"), "_8005ABC")

    def test_namespace_keeps_dots(self):
        self.assertEqual(_escape_namespace("Unity.VisualScripting"), "Unity.VisualScripting")
        self.assertEqual(_escape_namespace("Foo.lock"), "Foo.@lock")


class RenderTypeTests(unittest.TestCase):
    def test_basic_type_with_method(self):
        mt = MergedType(0, "Player", "Game", (
            MergedMethod(0, "Update", None, None, None),
        ), (), (), ())
        source = _render_type(mt, "Player")
        self.assertIn("namespace Game", source)
        self.assertIn("class Player", source)
        self.assertIn("void Update()", source)

    def test_method_with_rva(self):
        mt = MergedType(0, "Foo", "", (
            MergedMethod(0, "Bar", 0x12345, None, None),
        ), (), (), ())
        source = _render_type(mt, "Foo")
        self.assertIn("RVA: 0x12345", source)

    def test_method_with_pseudo_c(self):
        mt = MergedType(0, "Foo", "", (
            MergedMethod(0, "Baz", 0xABC, 0xABC, "int x = 42;\nreturn x;"),
        ), (), (), ())
        source = _render_type(mt, "Foo")
        self.assertIn("// int x = 42;", source)
        self.assertIn("// return x;", source)

    def test_fields_and_properties(self):
        mt = MergedType(0, "Data", "NS", (),
                        (MergedField(0, "health"),), (MergedProperty(0, "Name"),),
                        (MergedEvent(0, "OnDeath"),))
        source = _render_type(mt, "Data")
        self.assertIn("object health;", source)
        self.assertIn("object Name { get; set; }", source)
        self.assertIn("event global::System.Action OnDeath", source)

    def test_constructors(self):
        mt = MergedType(0, "Foo", "", (
            MergedMethod(0, ".ctor", None, None, None),
            MergedMethod(1, ".ctor", None, None, None),
            MergedMethod(2, ".cctor", None, None, None),
        ), (), (), ())
        source = _render_type(mt, "Foo")
        self.assertEqual(source.count("public Foo()"), 1)
        self.assertIn("static Foo()", source)
        self.assertIn("// .ctor (.ctor overload)", source)

    def test_accessors_do_not_collide_with_property(self):
        mt = MergedType(0, "Coll", "", (
            MergedMethod(0, "get_Count", None, None, None),
            MergedMethod(1, "System.Collections.ICollection.get_Count", None, None, None),
        ), (), (MergedProperty(0, "Count"), MergedProperty(1, "System.Collections.ICollection.Count")), ())
        source = _render_type(mt, "Coll")
        self.assertIn("public object Count { get; set; }", source)
        self.assertIn("public object Count_1 { get; set; }", source)
        self.assertNotIn("void get_Count", source)
        self.assertIn("accessor of Count", source)

    def test_overloads_and_keywords_are_unique(self):
        mt = MergedType(0, "Lock", "", (
            MergedMethod(0, "Run", None, None, None),
            MergedMethod(1, "Run", None, None, None),
            MergedMethod(2, "Lock", None, None, None),
        ), (MergedField(0, "try"), MergedField(1, "8005")), (), ())
        source = _render_type(mt, "Lock")
        self.assertIn("void Run()", source)
        self.assertIn("void Run_1()", source)
        self.assertIn("void Lock_1()", source)
        self.assertIn("object @try;", source)
        self.assertIn("object _8005;", source)


class MergeTests(unittest.TestCase):
    def test_merge_basic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp = Path(tmpdir) / "il2cpp"
            il2cpp.mkdir()
            (il2cpp / "types.tsv").write_text("0\tGame\tPlayer\n1\t\tGlobal\n")
            (il2cpp / "images.tsv").write_text("0\tAssembly-CSharp.dll\t0\t2\n")
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
            (il2cpp / "images.tsv").write_text("0\tAssembly-CSharp.dll\t0\t1\n")
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
            (il2cpp / "images.tsv").write_text("0\tAssembly-CSharp.dll\t0\t1\n")
            (il2cpp / "members.tsv").write_text("")
            out = Path(tmpdir) / "out"
            merge(il2cpp, None, None, out)
            import json
            report = json.loads((out / "merge_report.json").read_text())
            self.assertEqual(report["types"], 1)

    def test_engine_assemblies_skipped_and_collisions_kept(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp = Path(tmpdir) / "il2cpp"
            il2cpp.mkdir()
            (il2cpp / "types.tsv").write_text(
                "0\tSystem\tAction\n1\tUnityEngine\tMonoBehaviour\n"
                "2\tGame\tEnumerator\n3\tGame\tEnumerator\n4\tGame\tfoo\n5\tGame\tFoo\n")
            (il2cpp / "images.tsv").write_text(
                "0\tmscorlib.dll\t0\t1\n1\tUnityEngine.CoreModule.dll\t1\t1\n2\tAssembly-CSharp.dll\t2\t4\n")
            (il2cpp / "members.tsv").write_text("")
            out = Path(tmpdir) / "out"
            result = merge(il2cpp, None, None, out)
            self.assertEqual(result.engine_types_skipped, 2)
            self.assertFalse((out / "System").exists())
            self.assertFalse((out / "UnityEngine").exists())
            names = sorted(p.name for p in (out / "Game").glob("*.cs"))
            self.assertEqual(names, ["Enumerator.cs", "Enumerator_1.cs", "Foo_1.cs", "foo.cs"])
            self.assertIn("public class Enumerator_1", (out / "Game" / "Enumerator_1.cs").read_text())

    def test_compiler_and_unity_generated_types_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp = Path(tmpdir) / "il2cpp"
            il2cpp.mkdir()
            generated = ["<Module>", "<PrivateImplementationDetails>", "<>c", "<Load>d__3",
                         "__StaticArrayInitTypeSize=16", "UnitySourceGeneratedAssemblyMonoScriptTypes_v1"]
            rows = [f"{i}\t\t{name}" for i, name in enumerate(generated + ["Player"])]
            (il2cpp / "types.tsv").write_text("\n".join(rows) + "\n")
            (il2cpp / "images.tsv").write_text(f"0\tAssembly-CSharp.dll\t0\t{len(rows)}\n")
            (il2cpp / "members.tsv").write_text("")
            out = Path(tmpdir) / "out"
            result = merge(il2cpp, None, None, out)
            self.assertEqual(result.generated_types_skipped, len(generated))
            self.assertEqual([p.name for p in (out / "_global").glob("*.cs")], ["Player.cs"])

    def test_missing_images_table_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp = Path(tmpdir) / "il2cpp"
            il2cpp.mkdir()
            (il2cpp / "types.tsv").write_text("0\tGame\tPlayer\n")
            with self.assertRaises(UareError):
                merge(il2cpp, None, None, Path(tmpdir) / "out")


if __name__ == "__main__":
    unittest.main()


class EnrichFormatJoinTests(unittest.TestCase):
    """`uare il2cpp-enrich` rows (rva, class, method, signature) are joined on type and method name."""

    def _tables(self, tmpdir: str, members: str, enrich_rows: str) -> tuple[Path, Path]:
        il2cpp = Path(tmpdir) / "il2cpp"
        il2cpp.mkdir()
        (il2cpp / "types.tsv").write_text("0\tNS\tFoo\n1\tNS\tBar\n")
        (il2cpp / "images.tsv").write_text("0\tAssembly-CSharp.dll\t0\t1\n")
        (il2cpp / "members.tsv").write_text(members)
        enrich = Path(tmpdir) / "enrich"
        enrich.mkdir()
        (enrich / "methods_rva.tsv").write_text(enrich_rows)
        return il2cpp, enrich

    def test_unique_name_links_rva(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp, enrich = self._tables(
                tmpdir,
                "0\tFoo\tmethod\t10\tRun\n1\tBar\tmethod\t11\tRun\n",
                "0x1000\tNS.Foo\tRun\tvoid Run()\n0x2000\tNS.Bar\tRun\tvoid Run(int)\n",
            )
            from uare.il2cpp_merge import _load_rvas, _load_types, _load_members
            rvas = _load_rvas(enrich, _load_types(il2cpp), _load_members(il2cpp))
            self.assertEqual(rvas, {10: 0x1000, 11: 0x2000})

    def test_ambiguous_overloads_stay_unlinked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp, enrich = self._tables(
                tmpdir,
                "0\tFoo\tmethod\t10\tRun\n0\tFoo\tmethod\t12\tRun\n",
                "0x1000\tNS.Foo\tRun\tvoid Run()\n0x3000\tNS.Foo\tRun\tvoid Run(int)\n",
            )
            from uare.il2cpp_merge import _load_rvas, _load_types, _load_members
            rvas = _load_rvas(enrich, _load_types(il2cpp), _load_members(il2cpp))
            self.assertEqual(rvas, {})

    def test_index_format_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp, enrich = self._tables(tmpdir, "", "7\t4096\n")
            from uare.il2cpp_merge import _load_rvas, _load_types, _load_members
            rvas = _load_rvas(enrich, _load_types(il2cpp), _load_members(il2cpp))
            self.assertEqual(rvas, {7: 4096})


class OverloadPairingTests(unittest.TestCase):
    """Overloads sharing a type and name are linked by parameter count when that identifies them."""

    def test_signature_parameter_count_ignores_this_and_method_info(self):
        from uare.il2cpp_merge import _signature_parameter_count
        self.assertEqual(_signature_parameter_count("void T_M (T_o* __this, int32_t a, const MethodInfo* method);"), 1)
        self.assertEqual(_signature_parameter_count("void T_S (const MethodInfo* method);"), 0)
        self.assertEqual(_signature_parameter_count("void T_S (int32_t a, int32_t b, const MethodInfo* method);"), 2)
        self.assertIsNone(_signature_parameter_count("not a signature"))

    def test_overloads_link_by_parameter_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp = Path(tmpdir) / "il2cpp"
            il2cpp.mkdir()
            (il2cpp / "types.tsv").write_text("0\tNS\tFoo\n")
            (il2cpp / "members.tsv").write_text("0\tFoo\tmethod\t10\tRun\n0\tFoo\tmethod\t11\tRun\n")
            (il2cpp / "method_parameters.tsv").write_text("10\t0\n11\t1\n")
            enrich = Path(tmpdir) / "enrich"
            enrich.mkdir()
            (enrich / "methods_rva.tsv").write_text(
                "0x1000\tNS.Foo\tRun\tvoid NS_Foo_Run (NS_Foo_o* __this, const MethodInfo* method);\n"
                "0x2000\tNS.Foo\tRun\tvoid NS_Foo_Run (NS_Foo_o* __this, int32_t a, const MethodInfo* method);\n")
            from uare.il2cpp_merge import _load_rvas, _load_types, _load_members, _load_parameter_counts
            rvas = _load_rvas(enrich, _load_types(il2cpp), _load_members(il2cpp), _load_parameter_counts(il2cpp))
            self.assertEqual(rvas, {10: 0x1000, 11: 0x2000})

    def test_overloads_with_equal_counts_stay_unlinked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            il2cpp = Path(tmpdir) / "il2cpp"
            il2cpp.mkdir()
            (il2cpp / "types.tsv").write_text("0\tNS\tFoo\n")
            (il2cpp / "members.tsv").write_text("0\tFoo\tmethod\t10\tRun\n0\tFoo\tmethod\t11\tRun\n")
            (il2cpp / "method_parameters.tsv").write_text("10\t1\n11\t1\n")
            enrich = Path(tmpdir) / "enrich"
            enrich.mkdir()
            (enrich / "methods_rva.tsv").write_text(
                "0x1000\tNS.Foo\tRun\tvoid NS_Foo_Run (NS_Foo_o* __this, int32_t a, const MethodInfo* method);\n"
                "0x2000\tNS.Foo\tRun\tvoid NS_Foo_Run (NS_Foo_o* __this, int32_t b, const MethodInfo* method);\n")
            from uare.il2cpp_merge import _load_rvas, _load_types, _load_members, _load_parameter_counts
            rvas = _load_rvas(enrich, _load_types(il2cpp), _load_members(il2cpp), _load_parameter_counts(il2cpp))
            self.assertEqual(rvas, {})
