import errno
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertTrue((out / "shared/textures/sprite/pet/pet/1009/icon.png").is_file())
        self.assertTrue((out / "shared/meshes/sprite/pet/pet/1009/car.obj").is_file())
        self.assertTrue((out / "shared/textassets/sprite/pet/pet/1009/config.txt").is_file())

    def test_duplicate_names_are_kept(self) -> None:
        _result, out = self._package()
        icons = sorted(p.name for p in (out / "shared/textures/sprite/pet/pet/1009").glob("icon*.png"))
        self.assertEqual(len(icons), 2)

    def test_unclassified_hierarchy_nests_components_without_claiming_prefab(self) -> None:
        _result, out = self._package()
        self.assertFalse((out / "prefabs").exists())
        prefab = next((out / "evidence/unclassified/hierarchies").glob("**/*.json"))
        roots = json.loads(prefab.read_text(encoding="utf-8"))["roots"]
        self.assertEqual(roots[0]["name"], "Root")
        self.assertEqual(roots[0]["children"][0]["name"], "Child")

    def test_fev_bank_is_copied_and_decoded(self) -> None:
        _result, out = self._package()
        bank = out / "shared/audio/sprite/pet/pet/1009/Arrow.bank"
        self.assertTrue(bank.is_file())
        self.assertTrue((out / "shared/audio/sprite/pet/pet/1009/Arrow/Arrow.wav").is_file())

    def test_uncategorised_objects_fall_back_to_typetree_json(self) -> None:
        _result, out = self._package()
        self.assertTrue(list((out / "evidence/unclassified/objects/Cubemap").glob("**/sky.json")))

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

    def test_package_model_keeps_serialized_file_scope_for_duplicate_path_ids(self) -> None:
        from uare.game_model import identity, load_model
        first = _Obj("MonoBehaviour", 1, "Controller", _Pay(), typetree={"target": {"m_FileID": 1, "m_PathID": 1}})
        second = _Obj("Material", 1, "Material", _Pay(), typetree={"m_Name": "Material"})
        first.assets_file = _Pay(name="CAB-A", externals=[_Pay(path="archive:/CAB-B/CAB-B")])
        second.assets_file = _Pay(name="CAB-B", externals=[])
        ENVS[b"sprite"] = [first, second]
        _result, out = self._package()
        model = load_model(out / "game_model.json")
        self.assertEqual(len(model.objects), 2)
        source = model.objects[0].source
        edge = next(r for r in model.references if r.field == "target")
        self.assertEqual(edge.source_id, identity(source, "CAB-A", 1))
        self.assertEqual(edge.target_id, identity(source, "CAB-B", 1))
        catalog = json.loads((out / "catalog.json").read_text())
        self.assertEqual(len({row["object_id"] for row in catalog}), 2)

    def test_run_fails_before_pulling_when_unitypy_missing(self) -> None:
        sys.modules["UnityPy"] = None
        workdir = self.tmp / "work"
        args = types.SimpleNamespace(workdir=str(workdir), package="com.example", serial=None, adb=None,
                                     timeout=10, pull_timeout=10, retries=0)
        with self.assertRaises(pt.UnityPyRequired):
            cli.cmd_run(args)
        self.assertFalse(workdir.exists())


ATLAS_TEXT = (
    "hero.png\nsize: 4,4\nformat: RGBA8888\nfilter: Linear,Linear\nrepeat: none\n"
    "body\n  rotate: false\n  xy: 0, 0\n  size: 4, 4\n  orig: 4, 4\n  offset: 0, 0\n  index: -1\n\n"
    "hero mid.png\nsize: 4,4\nformat: RGBA8888\nfilter: Linear,Linear\nrepeat: none\n"
    "mid\n  rotate: false\n  xy: 0, 0\n  size: 4, 4\n  orig: 4, 4\n  offset: 0, 0\n  index: -1\n\n"
    "missing.png\nsize: 2,2\nformat: RGBA8888\nfilter: Linear,Linear\nrepeat: none\n"
    "tail\n  rotate: false\n  xy: 0, 0\n  size: 2, 2\n  orig: 2, 2\n  offset: 0, 0\n  index: -1\n"
)


def _ptr(path_id: int) -> dict:
    return {"m_FileID": 0, "m_PathID": path_id}


def _in_file(serialized: str, objects: list) -> list:
    """Assign fake objects to one serialized file, the way UnityPy groups objects by file."""
    for obj in objects:
        obj.assets_file = _Pay(name=serialized, externals=[])
    return objects


def _container(pairs: list[tuple[str, int]]) -> _Obj:
    typetree = {"m_Container": [[path, {"asset": _ptr(target), "preloadIndex": 0, "preloadSize": 0}]
                                for path, target in pairs]}
    return _Obj("AssetBundle", 100, "bundle", _Pay(), typetree=typetree)


def _gameobject(path_id: int, name: str, transform_id: int) -> _Obj:
    payload = _Pay(m_Component=[_PPtr(transform_id)], m_Name=name)
    typetree = {"m_Name": name, "m_Component": [{"component": _ptr(transform_id)}]}
    return _Obj("GameObject", path_id, name, payload, typetree=typetree)


def _transform(path_id: int, gameobject_id: int) -> _Obj:
    payload = _Pay(m_GameObject=_PPtr(gameobject_id), m_Father=_PPtr(0))
    return _Obj("Transform", path_id, "", payload,
                typetree={"m_GameObject": _ptr(gameobject_id), "m_Father": _ptr(0), "m_Children": []})


def _scene_objects(container_path: str) -> list:
    holder = _Obj("MonoScript", 9, "Holder", _Pay(m_ClassName="Holder"),
                  typetree={"m_ClassName": "Holder", "m_Namespace": "", "m_AssemblyName": "Game.dll"})
    behaviour = _Obj("MonoBehaviour", 8, "", _Pay(m_Script=_PPtr(9, _Pay(m_ClassName="Holder"))),
                     typetree={"m_Script": _ptr(9), "sprite": _ptr(6), "placeholder": _ptr(7)})
    return [
        _container([(container_path, 1)]),
        _gameobject(1, "Root", 2), _transform(2, 1),
        behaviour, holder,
        _Obj("Texture2D", 6, "icon", _Pay(m_Width=4, m_Height=4, image=_Image())),
        _Obj("Texture2D", 7, "placeholder", _Pay(m_Width=0, m_Height=0, image=None)),
    ]


def _empty_scene_objects(container_path: str) -> list:
    return [_container([(container_path, 5)]),
            _Obj("RenderSettings", 5, "", _Pay(), typetree={"m_Fog": False})]


def _spine_objects() -> list:
    script = _Obj("MonoScript", 30, "SkeletonDataAsset", _Pay(m_ClassName="SkeletonDataAsset"),
                  typetree={"m_ClassName": "SkeletonDataAsset", "m_Namespace": "Spine.Unity", "m_AssemblyName": "spine-unity"})
    atlas_script = _Obj("MonoScript", 36, "SpineAtlasAsset", _Pay(m_ClassName="SpineAtlasAsset"),
                        typetree={"m_ClassName": "SpineAtlasAsset", "m_Namespace": "Spine.Unity", "m_AssemblyName": "spine-unity"})
    anchor = _Obj("MonoBehaviour", 31, "hero_SkeletonData",
                  _Pay(m_Script=_PPtr(30, _Pay(m_ClassName="SkeletonDataAsset"))),
                  typetree={"m_Script": _ptr(30), "skeletonJSON": _ptr(32), "atlasAssets": [_ptr(33)]})
    atlas_asset = _Obj("MonoBehaviour", 33, "",
                       _Pay(m_Script=_PPtr(36, _Pay(m_ClassName="SpineAtlasAsset"))),
                       typetree={"m_Script": _ptr(36), "atlasFile": _ptr(34)})
    return [
        script, atlas_script, anchor, atlas_asset,
        _Obj("TextAsset", 32, "hero.skel", _Pay(m_Script=b"SKELETON")),
        _Obj("TextAsset", 34, "hero.atlas", _Pay(m_Script=ATLAS_TEXT.encode("utf-8"))),
        _Obj("Texture2D", 35, "hero", _Pay(m_Width=4, m_Height=4, image=_Image())),
        _Obj("Texture2D", 37, "hero mid", _Pay(m_Width=4, m_Height=4, image=_Image())),
    ]


class LayoutTests(unittest.TestCase):
    """Scene, prefab, spine and hard-link layout of the package output."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "Data"
        self.data.mkdir()
        self._saved = {name: sys.modules.get(name) for name in ("UnityPy", "PIL", "fmod_toolkit")}
        sys.modules.update(_fake_modules())
        ENVS.clear()
        ENVS[b"empty"] = []
        (self.data / "globalgamemanagers").write_bytes(SERIALIZED_TAG + b"empty")

    def tearDown(self) -> None:
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self._tmp.cleanup()

    def _bundle(self, label: str, objects: list) -> None:
        ENVS[label.encode("utf-8")] = objects
        name = f"gamepackage_assets_gameassets_{label}_" + "e" * 32 + ".bundle"
        (self.data / name).write_bytes(b"UnityFS\x00" + label.encode("utf-8"))

    def _package(self) -> tuple[pt.PackageResult, Path]:
        out = self.tmp / "out"
        return pt.package(self.data, out), out

    def test_scenes_take_their_container_name_and_empty_scenes_keep_a_folder(self) -> None:
        self._bundle("scenes", _in_file("CAB-map", _scene_objects("Assets/GameAssets/Scenes/Map_1001_2.unity"))
                     + _in_file("CAB-empty", _empty_scene_objects("Assets/GameAssets/Scenes/Empty.unity")))
        self._bundle("otherscenes", _in_file("CAB-other", _scene_objects("Assets/Other/Map_1001_2.unity")))
        result, out = self._package()
        self.assertEqual(sorted(p.name for p in (out / "scenes").iterdir()), ["Empty", "Map_1001_2", "Map_1001_2_1"])
        for name in ("Map_1001_2", "Map_1001_2_1"):
            scene = json.loads((out / "scenes" / name / "scene.json").read_text(encoding="utf-8"))
            self.assertEqual(scene["roots"][0]["name"], "Root")
        empty = json.loads((out / "scenes/Empty/scene.json").read_text(encoding="utf-8"))
        self.assertEqual(empty["roots"], [])
        self.assertEqual(result.prefabs, 0)

    def test_prefab_roots_are_split_and_unclaimed_roots_stay_in_evidence(self) -> None:
        objects = [
            _container([("Assets/ArtAssets/fx/A.prefab", 1), ("Assets/ArtAssets/fx/B.prefab", 10)]),
            _gameobject(1, "A", 2), _transform(2, 1),
            _gameobject(10, "B", 11), _transform(11, 10),
            _gameobject(20, "Loose", 21), _transform(21, 20),
        ]
        self._bundle("artassets_fx", _in_file("CAB-fx", objects))
        result, out = self._package()
        prefab_a = json.loads(next(out.glob("prefabs/**/A/prefab.json")).read_text(encoding="utf-8"))
        prefab_b = json.loads(next(out.glob("prefabs/**/B/prefab.json")).read_text(encoding="utf-8"))
        self.assertEqual([r["name"] for r in prefab_a["roots"]], ["A"])
        self.assertEqual([r["name"] for r in prefab_b["roots"]], ["B"])
        loose = json.loads(next(out.glob("evidence/unclassified/hierarchies/**/*.json")).read_text(encoding="utf-8"))
        self.assertEqual([r["name"] for r in loose["roots"]], ["Loose"])
        catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
        outputs = {e["name"]: e["output"] for e in catalog if e["class_name"] == "GameObject"}
        self.assertTrue(outputs["A"].endswith("/A/prefab.json"), outputs["A"])
        self.assertTrue(outputs["Loose"].startswith("evidence/unclassified/hierarchies/"), outputs["Loose"])
        self.assertEqual(result.prefabs, 2)

    def test_scene_folder_hard_links_its_shared_assets_and_manifest_lists_unexported_ones(self) -> None:
        self._bundle("scenes", _in_file("CAB-map", _scene_objects("Assets/GameAssets/Scenes/Map_1001_2.unity")))
        result, out = self._package()
        folder = out / "scenes/Map_1001_2"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        icon = next(a for a in manifest["assets"] if a["path"].endswith("icon.png"))
        self.assertTrue(icon["shared"].startswith("shared/textures/"), icon)
        linked = folder / icon["path"]
        self.assertTrue(os.path.samefile(linked, out / icon["shared"]))
        self.assertGreaterEqual(linked.stat().st_nlink, 2)
        self.assertTrue(any("placeholder" in row["note"] for row in manifest["without_output"]),
                        manifest["without_output"])
        self.assertGreaterEqual(result.links, 1)
        self.assertGreaterEqual(result.most_links_per_file, 1)

    def test_spine_folder_links_skeleton_atlas_pages_and_dump_and_reports_missing_pages(self) -> None:
        self._bundle("spine", _in_file("CAB-spine", _spine_objects()))
        result, out = self._package()
        folder = next(out.glob("spines/**/hero_SkeletonData"))
        self.assertTrue((folder / "hero.skel").is_file())
        self.assertTrue((folder / "hero.atlas").is_file())
        self.assertTrue((folder / "hero.png").is_file())
        self.assertTrue((folder / "hero_SkeletonData.json").is_file())
        self.assertFalse((folder / "missing.png").exists())
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        status = {row["name"]: row["status"] for row in manifest["pages"]}
        self.assertEqual(status, {"hero.png": "linked", "hero mid.png": "linked", "missing.png": "unresolved"})
        self.assertTrue((folder / "hero mid.png").is_file())
        self.assertTrue(os.path.samefile(folder / "hero.png", out / manifest["pages"][0]["shared"]))
        self.assertEqual(result.spines, 1)

    def test_link_failure_is_loud_and_leaves_no_output(self) -> None:
        self._bundle("scenes", _in_file("CAB-map", _scene_objects("Assets/GameAssets/Scenes/Map_1001_2.unity")))
        real_link = os.link

        def failing(source, target, *args, **kwargs):
            if "scenes" in str(target):
                raise OSError(errno.EMLINK, "too many links")
            return real_link(source, target, *args, **kwargs)

        out = self.tmp / "out"
        with mock.patch("uare.package_tree.os.link", side_effect=failing):
            with self.assertRaises(pt.PackageLinkError):
                pt.package(self.data, out)
        self.assertFalse(out.exists())

    def test_preflight_refuses_a_volume_without_hard_links_before_any_export(self) -> None:
        self._bundle("scenes", _in_file("CAB-map", _scene_objects("Assets/GameAssets/Scenes/Map_1001_2.unity")))
        out = self.tmp / "out"
        with mock.patch("uare.package_tree.os.link", side_effect=PermissionError(errno.EPERM, "not permitted")):
            with self.assertRaises(pt.PackageLinkError):
                pt.package(self.data, out)
        self.assertFalse(out.exists())

    def test_font_payload_given_as_byte_list_is_written_as_bytes(self) -> None:
        ttf = [0, 1, 0, 0, 0, 16, 0, 0]
        self._bundle("fonts", _in_file("CAB-font", [_Obj("Font", 40, "Sample", _Pay(m_FontData=ttf))]))
        _result, out = self._package()
        font = next((out / "shared/fonts").rglob("Sample.ttf"))
        self.assertEqual(font.read_bytes(), bytes(ttf))

    def test_unsupported_binary_payload_is_recorded_as_unclassified(self) -> None:
        self._bundle("fonts", _in_file("CAB-font", [_Obj("Font", 40, "Broken", _Pay(m_FontData={"raw": 1}))]))
        _result, out = self._package()
        catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
        entry = next(e for e in catalog if e["name"] == "Broken")
        self.assertIsNone(entry["output"])
        self.assertIn("unsupported", entry["note"])

    def test_atlas_pages_are_the_name_lines_of_page_blocks_only(self) -> None:
        self.assertEqual(pt._atlas_pages(ATLAS_TEXT), ["hero.png", "hero mid.png", "missing.png"])


if __name__ == "__main__":
    unittest.main()
