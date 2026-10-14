import importlib.util
import struct
import sys
from dataclasses import dataclass
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from uare.script_types import ScriptTypesError, align_monobehaviour_header, collect_assemblies, load_script_types


@dataclass
class _Entry:
    """The flat node shape TypeTreeGeneratorAPI returns."""
    m_Level: int
    m_Type: str
    m_Name: str
    m_MetaFlag: int = 0


def _monobehaviour_entries() -> list[_Entry]:
    pptr = lambda name, kind: [_Entry(1, kind, name), _Entry(2, "int", "m_FileID"), _Entry(2, "SInt64", "m_PathID")]
    return ([_Entry(0, "MonoBehaviour", "Base")] + pptr("m_GameObject", "PPtr<GameObject>") +
            [_Entry(1, "UInt8", "m_Enabled")] + pptr("m_Script", "PPtr<MonoScript>") +
            [_Entry(1, "string", "m_Name", 0x4000), _Entry(2, "Array", "Array", 0x4000),
             _Entry(3, "int", "size"), _Entry(3, "char", "data"), _Entry(1, "int", "m_Value")])


class ScriptTypesTests(unittest.TestCase):
    def test_first_directory_wins_on_assembly_name_clashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a", Path(tmp) / "b"
            (first / "sub").mkdir(parents=True)
            second.mkdir()
            (first / "sub" / "UnityEngine.UI.dll").write_bytes(b"MZ1")
            (second / "UnityEngine.UI.dll").write_bytes(b"MZ2")
            (second / "Unity.TextMeshPro.dll").write_bytes(b"MZ3")
            paths, shadowed = collect_assemblies([first, second])
        self.assertEqual(sorted(p.name for p in paths), ["Unity.TextMeshPro.dll", "UnityEngine.UI.dll"])
        self.assertIn("/a/sub/UnityEngine.UI.dll", next(str(p) for p in paths if p.name == "UnityEngine.UI.dll"))
        self.assertEqual(len(shadowed), 1)

    def test_missing_or_empty_directories_fail_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ScriptTypesError, "does not exist"):
                collect_assemblies([Path(tmp) / "absent"])
            with self.assertRaisesRegex(ScriptTypesError, "No .dll files"):
                collect_assemblies([Path(tmp)])

    @unittest.skipUnless(importlib.util.find_spec("UnityPy"), "UnityPy is not installed")
    def test_aligned_header_reads_real_bytes_through_unitypy(self) -> None:
        from UnityPy.helpers.TypeTreeHelper import read_typetree
        from UnityPy.helpers.TypeTreeNode import TypeTreeNode
        from UnityPy.streams import EndianBinaryReader
        raw = (struct.pack("<iq", 0, 7) + b"\x01\x00\x00\x00" + struct.pack("<iq", 1, 3769)
               + struct.pack("<i", 0) + struct.pack("<i", 42))
        entries = align_monobehaviour_header(_monobehaviour_entries())
        # Built the way UnityPy's TypeTreeGenerator.get_nodes_up builds C nodes from the flat entries.
        root = TypeTreeNode.from_list([TypeTreeNode(e.m_Level, e.m_Type, e.m_Name, 0, 0, m_MetaFlag=e.m_MetaFlag)
                                       for e in entries])
        data = read_typetree(root, EndianBinaryReader(raw, "<"), byte_size=len(raw))
        self.assertEqual(data["m_Script"], {"m_FileID": 1, "m_PathID": 3769})
        self.assertEqual(data["m_Value"], 42)
        self.assertEqual([e.m_MetaFlag for e in entries if e.m_Level == 1],
                         [0, 0x4000, 0, 0x4000, 0])

    def test_missing_generator_package_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"TypeTreeGeneratorAPI": None}):
            (Path(tmp) / "UnityEngine.UI.dll").write_bytes(b"MZ")
            with self.assertRaisesRegex(ScriptTypesError, "TypeTreeGeneratorAPI"):
                load_script_types("6000.2.15f1", [Path(tmp)])


if __name__ == "__main__":
    unittest.main()
