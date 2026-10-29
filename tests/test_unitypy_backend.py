import sys
import tempfile
import types
import unittest
from pathlib import Path

from fixtures import serialized_v22
from uare import unitypy_backend


class _FakeType:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeImage:
    def save(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG")


class _Pay:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class _FakeObj:
    def __init__(self, type_name: str, path_id: int, name: str, payload) -> None:
        self.type = _FakeType(type_name)
        self.path_id = path_id
        self.byte_size = 12
        self.container = None
        self._name = name
        self._payload = payload

    def peek_name(self) -> str:
        return self._name

    def read(self):
        return self._payload

    def read_typetree(self):
        return {"m_Value": 42, "m_Name": self._name}


class _FakeEnv:
    def __init__(self, objects) -> None:
        self.objects = objects


def _fake_unitypy():
    module = types.ModuleType("UnityPy")

    def load(path):
        return _FakeEnv([
            _FakeObj("TextAsset", 1, "Config", _Pay(m_Script=b"Id,value\n0,7\n")),
            _FakeObj("Texture2D", 2, "icon_coin", _Pay(image=_FakeImage())),
            _FakeObj("AudioClip", 3, "bgm", _Pay(samples={"bgm.wav": b"RIFFdata"})),
            _FakeObj("Mesh", 4, "car", _Pay(export=lambda: b"v 0 0 0\n")),
            _FakeObj("MonoBehaviour", 5, "Graph", _Pay()),
            _FakeObj("MonoScript", 6, "ShopPage", _Pay(m_ClassName="ShopPage", m_Namespace="Game.Shop",
                                                      m_AssemblyName="Assembly-CSharp")),
            _FakeObj("GameObject", 7, "", _Pay()),
        ])

    module.load = load
    return module


class UnityPyBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "Data"
        self.data.mkdir()
        (self.data / "globalgamemanagers").write_bytes(b"gg")
        (self.data / "sharedassets0.assets").write_bytes(serialized_v22("6000.2.15f1", 13, False, 3, 4096))
        self._saved = sys.modules.get("UnityPy")
        sys.modules["UnityPy"] = _fake_unitypy()

    def tearDown(self) -> None:
        if self._saved is None:
            sys.modules.pop("UnityPy", None)
        else:
            sys.modules["UnityPy"] = self._saved
        self._tmp.cleanup()

    def test_available_when_module_importable(self) -> None:
        self.assertTrue(unitypy_backend.available())

    def test_exports_all_media_kinds(self) -> None:
        out = self.tmp / "out"
        result = unitypy_backend.export_assets(self.data, out)
        self.assertEqual(result.objects, 7)
        self.assertEqual(result.text_assets, 1)
        self.assertEqual(result.textures, 1)
        self.assertEqual(result.audio, 1)
        self.assertEqual(result.meshes, 1)
        self.assertEqual(result.monobehaviours, 1)
        self.assertEqual(result.class_counts["GameObject"], 1)

        self.assertEqual((out / "textassets/Config").read_bytes(), b"Id,value\n0,7\n")
        self.assertTrue((out / "textures/icon_coin.png").is_file())
        self.assertEqual((out / "audio/bgm.wav").read_bytes(), b"RIFFdata")
        self.assertEqual((out / "meshes/car.obj").read_bytes(), b"v 0 0 0\n")
        self.assertIn("m_Value", (out / "monobehaviours/Graph.json").read_text())
        mono = (out / "monoscripts.tsv").read_text().strip().split("\t")
        self.assertEqual(mono[2:], ["ShopPage", "ShopPage", "Game.Shop", "Assembly-CSharp"])

    def test_cli_auto_backend_uses_unitypy(self) -> None:
        from uare.cli import main
        self.assertEqual(main(["export-assets", str(self.data), "--out", str(self.tmp / "out")]), 0)
        self.assertTrue((self.tmp / "out/textures/icon_coin.png").is_file())
        self.assertTrue((self.tmp / "out/objects.jsonl").is_file())

    def test_textasset_with_surrogates_round_trips(self) -> None:
        module = types.ModuleType("UnityPy")
        module.load = lambda path: _FakeEnv([_FakeObj("TextAsset", 1, "Raw", _Pay(m_Script="\udcf7abc"))])
        saved = sys.modules.get("UnityPy")
        sys.modules["UnityPy"] = module
        try:
            out = self.tmp / "out2"
            result = unitypy_backend.export_assets(self.data, out)
            self.assertEqual(result.text_assets, 1)
            self.assertEqual((out / "textassets/Raw").read_bytes(), b"\xf7abc")
        finally:
            if saved is None:
                sys.modules.pop("UnityPy", None)
            else:
                sys.modules["UnityPy"] = saved

    def test_same_named_audio_clips_keep_their_disambiguated_file_names(self) -> None:
        out = self.tmp / "audio"
        out.mkdir()
        used: set[str] = set()
        for path_id, payload in ((7, b"RIFF1"), (8, b"RIFF2")):
            clip = _FakeObj("AudioClip", path_id, "bgm", _Pay(samples={"bgm.wav": payload}))
            unitypy_backend._export_audio(clip, "bgm", path_id, out, used)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["bgm.8.wav", "bgm.wav"])
        self.assertEqual((out / "bgm.wav").read_bytes(), b"RIFF1")

    def test_forced_stdlib_backend_ignores_unitypy(self) -> None:
        from uare.cli import main
        self.assertEqual(
            main(["export-assets", str(self.data), "--out", str(self.tmp / "out"), "--backend", "stdlib"]), 0)
        self.assertFalse(list((self.tmp / "out").glob("textures")))


if __name__ == "__main__":
    unittest.main()
