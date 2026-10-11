import sys
import tempfile
import types
import unittest
from pathlib import Path

from uare import package_tree as pt


class _PPtr:
    def __init__(self, path_id, target=None):
        self.path_id = path_id
        self._target = target

    def read(self):
        return self._target


class _Pay:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Image:
    def save(self, path):
        Path(path).write_bytes(b"\x89PNG")


class _Type:
    def __init__(self, name):
        self.name = name


class _Obj:
    def __init__(self, type_name, path_id, name, payload):
        self.type = _Type(type_name)
        self.path_id = path_id
        self.byte_size = 1
        self._name = name
        self._payload = payload

    def peek_name(self):
        return self._name

    def read(self):
        return self._payload

    def read_typetree(self):
        return {"m_Value": 1}


class _Env:
    def __init__(self, objects):
        self.objects = objects


def _fake_unitypy():
    module = types.ModuleType("UnityPy")
    script_target = _Pay(m_ClassName="SkeletonAnimation")
    module.load = lambda data: _Env([
        _Obj("GameObject", 1, "Root", _Pay()),
        _Obj("Transform", 2, "", _Pay(m_GameObject=_PPtr(1), m_Father=_PPtr(0))),
        _Obj("Transform", 3, "", _Pay(m_GameObject=_PPtr(1), m_Father=_PPtr(2))),
        _Obj("GameObject", 4, "Child", _Pay()),
        _Obj("TextAsset", 5, "config", _Pay(m_Script=b"a,b\n")),
        _Obj("TextAsset", 6, "pet.atlas", _Pay(m_Script=b"\npage\n")),
        _Obj("Texture2D", 7, "tex", _Pay(image=_Image())),
        _Obj("MonoBehaviour", 8, "", _Pay(m_Script=_PPtr(99, script_target))),
        _Obj("MonoScript", 9, "SkeletonAnimation",
             _Pay(m_ClassName="SkeletonAnimation", m_Namespace="Spine.Unity", m_AssemblyName="spine-unity")),
    ])
    return module


class BundleOffsetTests(unittest.TestCase):
    def test_detects_prefixed_bundle(self) -> None:
        self.assertEqual(pt.bundle_offset(b"\x00" * 36 + b"UnityFS\x00rest"), 36)

    def test_plain_bundle(self) -> None:
        self.assertEqual(pt.bundle_offset(b"UnityFS\x00rest"), 0)

    def test_no_bundle(self) -> None:
        self.assertIsNone(pt.bundle_offset(b"\x00" * 64))


class HierarchyTests(unittest.TestCase):
    def test_builds_paths(self) -> None:
        objs = [
            _Obj("GameObject", 1, "Root", _Pay()),
            _Obj("Transform", 2, "", _Pay(m_GameObject=_PPtr(1), m_Father=_PPtr(0))),
            _Obj("GameObject", 4, "Child", _Pay()),
            _Obj("Transform", 3, "", _Pay(m_GameObject=_PPtr(4), m_Father=_PPtr(2))),
        ]
        tree = pt._hierarchy(objs)
        paths = sorted(n["path"] for n in tree["objects"])
        self.assertEqual(paths, ["Root", "Root/Child"])


class PackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "Data"
        self.data.mkdir()
        (self.data / "globalgamemanagers").write_bytes(b"\x00" * 16)
        self._saved = sys.modules.get("UnityPy")
        sys.modules["UnityPy"] = _fake_unitypy()

    def tearDown(self) -> None:
        if self._saved is None:
            sys.modules.pop("UnityPy", None)
        else:
            sys.modules["UnityPy"] = self._saved
        self._tmp.cleanup()

    def test_packages_into_categories(self) -> None:
        out = self.tmp / "out"
        result = pt.package(self.data, out)
        self.assertEqual(result.objects, 9)
        self.assertEqual(result.categories.get("textures"), 1)
        self.assertEqual(result.categories.get("textassets"), 1)
        self.assertEqual(result.categories.get("spines"), 2)  # skeleton MonoBehaviour + pet.atlas
        self.assertTrue((out / "assets/textures/tex.png").is_file())
        self.assertTrue((out / "assets/textassets/config").is_file())
        self.assertTrue(list((out / "assets/spines").glob("*.json")))
        self.assertTrue(list(out.glob("hierarchies/*.json")))
        self.assertTrue((out / "catalog.json").is_file())
        self.assertTrue((out / "index.html").is_file())
        self.assertTrue((out / "scripts/monoscripts.tsv").is_file())


if __name__ == "__main__":
    unittest.main()
