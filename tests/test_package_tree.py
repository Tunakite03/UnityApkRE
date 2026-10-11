import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from uare import cli
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
    def __init__(self, type_name, path_id, name, payload, typetree=None):
        self.type = _Type(type_name)
        self.path_id = path_id
        self._name = name
        self._payload = payload
        self._typetree = typetree

    def peek_name(self):
        return self._name

    def read(self):
        return self._payload

    def read_typetree(self):
        if self._typetree is None:
            raise ValueError("no type tree")
        return self._typetree


class _Env:
    def __init__(self, objects):
        self.objects = objects


SERIALIZED_TAG = b"SERIAL\x00"
ENVS: dict[bytes, list] = {}


def _load(path):
    """Fake UnityPy.load: the tag written after the signature picks the object list."""
    data = Path(path).read_bytes()
    tag = data.split(b"\x00", 1)[1]
    return _Env(ENVS[tag])


def _fake_modules():
    unitypy = types.ModuleType("UnityPy")
    unitypy.load = _load
    pil = types.ModuleType("PIL")
    fmod = types.ModuleType("fmod_toolkit")

    def raw_to_wav(blob, name, channels, frequency):
        return {"Arrow.wav": b"RIFFwav"}

    fmod.raw_to_wav = raw_to_wav
    return {"UnityPy": unitypy, "PIL": pil, "fmod_toolkit": fmod}


def _fev_bank() -> bytes:
    """RIFF/FEV bank with an SND chunk that holds an FSB5 container (16 header bytes first)."""
    fsb = b"FSB5" + b"\x00" * 60
    snd = b"\x00" * 16 + fsb
    fmt = b"\x00" * 8
    body = b"FEV " + b"FMT " + len(fmt).to_bytes(4, "little") + fmt + b"SND " + len(snd).to_bytes(4, "little") + snd
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _bundle_objects() -> list:
    script_payload = _Pay(m_ClassName="SkeletonAnimation", m_Namespace="Spine.Unity", m_AssemblyName="spine-unity")
    script = _Obj("MonoScript", 9, "SkeletonAnimation", script_payload)
    return [
        _Obj("GameObject", 1, "Root", _Pay(m_Component=[_PPtr(2), _PPtr(3)], m_Name="Root")),
        _Obj("Transform", 2, "", _Pay(m_GameObject=_PPtr(1), m_Father=_PPtr(0))),
        _Obj("MeshFilter", 3, "", _Pay(), typetree={"m_Mesh": 1}),
        _Obj("GameObject", 4, "Child", _Pay(m_Component=[_PPtr(5)], m_Name="Child")),
        _Obj("Transform", 5, "", _Pay(m_GameObject=_PPtr(4), m_Father=_PPtr(2))),
        _Obj("Texture2D", 6, "icon", _Pay(m_Width=4, m_Height=4, image=_Image())),
        _Obj("Texture2D", 7, "placeholder", _Pay(m_Width=0, m_Height=0, image=None)),
        _Obj("MonoBehaviour", 8, "", _Pay(m_Script=_PPtr(9, script_payload)), typetree={"m_Value": 42}),
        script,
        _Obj("TextAsset", 10, "Arrow.bank", _Pay(m_Script=_fev_bank())),
        _Obj("TextAsset", 11, "config", _Pay(m_Script=b"a,b\n0,1\n")),
        _Obj("Mesh", 12, "car", _Pay(export=lambda: "v 0 0 0\n")),
        _Obj("Cubemap", 13, "sky", _Pay(), typetree={"m_Width": 256}),
        _Obj("Texture2D", 14, "icon", _Pay(m_Width=4, m_Height=4, image=_Image())),
    ]


class BundleHelperTests(unittest.TestCase):
    def test_detects_prefixed_bundle(self) -> None:
        self.assertEqual(pt.bundle_offset(b"\x00" * 36 + b"UnityFS\x00rest"), 36)

    def test_plain_bundle(self) -> None:
        self.assertEqual(pt.bundle_offset(b"UnityFS\x00rest"), 0)

    def test_no_bundle(self) -> None:
        self.assertIsNone(pt.bundle_offset(b"\x00" * 64))

    def test_group_drops_prefix_and_content_hash(self) -> None:
        path = Path("gamepackage_assets_gameassets_sprite_pet_pet_1009_" + "a" * 32 + ".bundle")
        self.assertEqual(pt._group_of(path, True), "sprite/pet/pet/1009")

    def test_group_strips_artassets_prefix(self) -> None:
        path = Path("gamepackage_assets_artassets_effect_prefabs_ui_arena_" + "b" * 32 + ".bundle")
        self.assertEqual(pt._group_of(path, True), "effect/prefabs/ui/arena")

    def test_group_strips_share_prefix(self) -> None:
        path = Path("gamepackage_share_assets_artassets_effect_spine_" + "c" * 32 + ".bundle")
        self.assertEqual(pt._group_of(path, True), "effect/spine")

    def test_serialized_files_share_one_group(self) -> None:
        self.assertEqual(pt._group_of(Path("level0"), False), "_serialized")

    def test_unique_keeps_source_extension(self) -> None:
        used: set[str] = set()
        self.assertEqual(pt._unique("arrow.atlas", 1, used), "arrow.atlas")
        self.assertEqual(pt._unique("arrow.atlas", 7, used), "arrow_1.atlas")

    def test_fsb_blobs_are_read_from_snd_chunk(self) -> None:
        blobs = pt._fsb_blobs(_fev_bank())
        self.assertEqual(len(blobs), 1)
        self.assertTrue(blobs[0].startswith(b"FSB5"))

    def test_require_unitypy_names_missing_module(self) -> None:
        saved = sys.modules.get("UnityPy")
        sys.modules["UnityPy"] = None  # import raises ImportError
        try:
            with self.assertRaises(pt.UnityPyRequired) as ctx:
                pt.require_unitypy()
            self.assertIn("UnityPy", str(ctx.exception))
        finally:
            if saved is None:
                sys.modules.pop("UnityPy", None)
            else:
                sys.modules["UnityPy"] = saved


class HierarchyTests(unittest.TestCase):
    def test_nests_children_and_inlines_components(self) -> None:
        objs = _bundle_objects()[:5]
        roots, homed, failures = pt._build_hierarchy(objs, exported={})
        self.assertEqual(failures, {})
        self.assertEqual([r["name"] for r in roots], ["Root"])
        self.assertEqual([c["name"] for c in roots[0]["children"]], ["Child"])
        mesh = next(c for c in roots[0]["components"] if c["class"] == "MeshFilter")
        self.assertEqual(mesh["data"], {"m_Mesh": 1})
        self.assertTrue({1, 2, 3, 4, 5} <= homed)


class PackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "Data"
        self.data.mkdir()
        self._saved = {name: sys.modules.get(name) for name in ("UnityPy", "PIL", "fmod_toolkit")}
        sys.modules.update(_fake_modules())
        ENVS.clear()
        ENVS[b"sprite"] = _bundle_objects()
        ENVS[b"empty"] = []
        bundle = "gamepackage_assets_gameassets_sprite_pet_pet_1009_" + "b" * 32 + ".bundle"
        (self.data / bundle).write_bytes(b"UnityFS\x00sprite")
        (self.data / ("gamepackage_assets_empty_" + "c" * 32 + ".bundle")).write_bytes(b"UnityFS\x00empty")
        (self.data / "level0").write_bytes(SERIALIZED_TAG + b"empty")
        (self.data / "globalgamemanagers").write_bytes(SERIALIZED_TAG + b"empty")

    def tearDown(self) -> None:
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self._tmp.cleanup()

    def _package(self) -> tuple[pt.PackageResult, Path]:
        out = self.tmp / "out"
        return pt.package(self.data, out), out

    def test_catalog_has_one_entry_per_object(self) -> None:
        result, out = self._package()
        catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(result.objects, len(ENVS[b"sprite"]))
        self.assertEqual(len(catalog), len(ENVS[b"sprite"]))
        for entry in catalog:
            self.assertTrue(entry["output"] or entry["note"], entry)
            if entry["output"] and entry["output"] != "scripts/monoscripts.tsv":
                self.assertTrue((out / entry["output"]).exists(), entry["output"])

    def test_outputs_are_grouped_by_source_bundle(self) -> None:
        _result, out = self._package()
        self.assertTrue((out / "assets/textures/sprite/pet/pet/1009/icon.png").is_file())
        self.assertTrue((out / "assets/meshes/sprite/pet/pet/1009/car.obj").is_file())
        self.assertTrue((out / "assets/textassets/sprite/pet/pet/1009/config.txt").is_file())

    def test_duplicate_names_are_kept(self) -> None:
        _result, out = self._package()
        icons = sorted(p.name for p in (out / "assets/textures/sprite/pet/pet/1009").glob("icon*.png"))
        self.assertEqual(len(icons), 2)

    def test_prefab_json_nests_components(self) -> None:
        _result, out = self._package()
        prefab = next((out / "assets/prefabs").glob("**/*.json"))
        roots = json.loads(prefab.read_text(encoding="utf-8"))["roots"]
        self.assertEqual(roots[0]["name"], "Root")
        self.assertEqual(roots[0]["children"][0]["name"], "Child")

    def test_fev_bank_is_copied_and_decoded(self) -> None:
        _result, out = self._package()
        bank = out / "assets/audio/sprite/pet/pet/1009/Arrow.bank"
        self.assertTrue(bank.is_file())
        self.assertTrue((out / "assets/audio/sprite/pet/pet/1009/Arrow/Arrow.wav").is_file())

    def test_uncategorised_objects_fall_back_to_typetree_json(self) -> None:
        _result, out = self._package()
        self.assertTrue(list((out / "assets/objects/Cubemap").glob("**/sky.json")))

    def test_monoscript_table_and_spine_guess(self) -> None:
        result, out = self._package()
        self.assertIn("SkeletonAnimation", (out / "scripts/monoscripts.tsv").read_text(encoding="utf-8"))
        self.assertEqual(result.categories.get("spines"), 1)

    def test_failing_bundle_is_reported_not_fatal(self) -> None:
        bad = self.data / ("gamepackage_assets_broken_" + "d" * 32 + ".bundle")
        bad.write_bytes(b"UnityFS\x00missing")
        result, out = self._package()
        self.assertEqual(result.errors, 1)
        self.assertTrue((out / "report.json").is_file())

    def test_run_fails_before_pulling_when_unitypy_missing(self) -> None:
        sys.modules["UnityPy"] = None
        workdir = self.tmp / "work"
        args = types.SimpleNamespace(workdir=str(workdir), package="com.example", serial=None, adb=None,
                                     timeout=10, pull_timeout=10, retries=0)
        with self.assertRaises(pt.UnityPyRequired):
            cli.cmd_run(args)
        self.assertFalse(workdir.exists())


if __name__ == "__main__":
    unittest.main()
