import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from uare.assetripper_remap import (Match, _sole_asset, apply_catalog, merge_catalog_duplicates, unmapped_moves, assetripper_fingerprint, catalog_identities,
                                    documents, match_export, near_pixels, prefab_tree, rewrite_project, scalar)
from uare.game_model import FileRecord, Json, ObjectRecord, identity, resolve

PREFAB = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &100
GameObject:
  m_Component:
  - component: {fileID: 400}
  - component: {fileID: 114500}
  m_Name: Fx_Hit01
--- !u!224 &400
RectTransform:
  m_GameObject: {fileID: 100}
  m_Children:
  - {fileID: 401}
  m_Father: {fileID: 0}
--- !u!114 &114500
MonoBehaviour:
  m_GameObject: {fileID: 100}
--- !u!1 &101
GameObject:
  m_Component:
  - component: {fileID: 401}
  m_Name: Spark
--- !u!224 &401
RectTransform:
  m_GameObject: {fileID: 101}
  m_Children: []
  m_Father: {fileID: 400}
"""

CLIP = """%YAML 1.1
--- !u!74 &7400000
AnimationClip:
  m_Name: Open
  m_SampleRate: 60
  m_AnimationClipSettings:
    serializedVersion: 2
    m_StopTime: 0.5
  m_Events:
  - time: 0.1
    functionName: OnOpen
"""

GUID_FILE = "000385f0e1866fd4cb29245d14652f36"
ANCHOR = "8f586378b4e144a9851e7b34d9b748ee"


def ptr(path_id: int) -> dict[str, Json]:
    return {"m_FileID": 0, "m_PathID": path_id}


def obj(file: str, pid: int, cls: str, data: dict[str, Json]) -> ObjectRecord:
    return ObjectRecord(identity("Data/" + file, file, pid), "Data/" + file, file, pid, cls,
                        str(data.get("m_Name", cls)), data, "complete", (), None)


def model(clip_name: str = "Open") -> object:
    objects = (
        obj(GUID_FILE, 1, "GameObject", {"m_Name": "Fx_Hit01", "m_Component": [{"component": ptr(2)}, {"component": ptr(3)}]}),
        obj(GUID_FILE, 2, "RectTransform", {"m_GameObject": ptr(1), "m_Father": ptr(0), "m_Children": [ptr(5)]}),
        obj(GUID_FILE, 3, "MonoBehaviour", {"m_GameObject": ptr(1)}),
        obj(GUID_FILE, 4, "GameObject", {"m_Name": "Spark", "m_Component": [{"component": ptr(5)}]}),
        obj(GUID_FILE, 5, "RectTransform", {"m_GameObject": ptr(4), "m_Father": ptr(2), "m_Children": []}),
        obj("sharedassets0.assets", 9, "AnimationClip", {"m_Name": clip_name, "m_SampleRate": 60,
                                                         "m_MuscleClip": {"m_StopTime": 0.5}, "m_Events": [{"functionName": "OnOpen"}]}),
        obj(ANCHOR, 1, "MonoBehaviour", {"m_Name": "LiberationSans SDF"}),
    )
    return resolve(tuple(FileRecord("Data/" + f, f, ()) for f in (GUID_FILE, "sharedassets0.assets", ANCHOR)), objects, ())


def export(root: Path) -> Path:
    project = root / "ExportedProject"
    (project / "Assets" / "Prefabs").mkdir(parents=True)
    (project / "Assets" / "AnimationClip").mkdir(parents=True)
    (project / "Assets" / "Prefabs" / "Fx_Hit01.prefab").write_text(PREFAB)
    (project / "Assets" / "Prefabs" / "Fx_Hit01.prefab.meta").write_text("fileFormatVersion: 2\nguid: " + "a" * 32 + "\n")
    (project / "Assets" / "AnimationClip" / "Open.anim").write_text(CLIP)
    (project / "Assets" / "AnimationClip" / "Open.anim.meta").write_text("fileFormatVersion: 2\nguid: " + "b" * 32 + "\n")
    (project / "Assets" / "Scene.unity").write_text("m_Prefab: {fileID: 100100000, guid: " + "a" * 32 + ", type: 3}\n")
    return project


class RemapTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("UnityPy"), "UnityPy names the component classes")
    def test_documents_and_prefab_tree_follow_hierarchy_order(self) -> None:
        self.assertEqual([(c, f) for c, f, _ in documents(PREFAB)][:2], [(1, 100), (224, 400)])
        key, order = prefab_tree(PREFAB)
        self.assertEqual(key, "Fx_Hit01|RectTransform,MonoBehaviour[Spark|RectTransform[]]")
        self.assertEqual(order, [100, 400, 114500, 101, 401])

    def test_quoted_scalars_are_decoded(self) -> None:
        self.assertEqual(scalar("'VFX Root '"), "VFX Root ")
        self.assertEqual(scalar("'it''s'"), "it's")
        self.assertEqual(scalar('"a\\"b"'), 'a"b')
        self.assertEqual(scalar("plain"), "plain")

    def test_sole_asset_rejects_a_file_whose_main_asset_is_another_object(self) -> None:
        font = obj(ANCHOR, 1, "MonoBehaviour", {"m_Name": "Anton SDF"})
        material = obj(ANCHOR, 2, "Material", {"m_Name": "Anton SDF Material"})
        atlas = obj(ANCHOR, 3, "Texture2D", {"m_Name": "Anton SDF Atlas"})
        self.assertFalse(_sole_asset(material, "Material", set(), [font, material, atlas]))
        sprite = obj(ANCHOR, 4, "Sprite", {"m_Name": "s"})
        self.assertTrue(_sole_asset(atlas, "Texture2D", set(), [atlas, sprite]))

    def test_clip_fingerprint_reads_settings_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Open.anim"
            path.write_text(CLIP)
            self.assertEqual(json.loads(assetripper_fingerprint(path, "AnimationClip")[0]), ["Open", "60", "0.5", ["OnOpen"]])

    @unittest.skipUnless(importlib.util.find_spec("UnityPy"), "UnityPy class names are needed for component classes")
    def test_prefab_gets_its_source_guid_and_file_ids_while_a_shared_clip_keeps_its_guid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = export(Path(tmp))
            matches = {m.path: m for m in match_export(project, model(), Path(tmp))}
            prefab = matches["Assets/Prefabs/Fx_Hit01.prefab"]
            self.assertEqual((prefab.status, prefab.original_guid), ("matched", GUID_FILE))
            self.assertEqual(prefab.file_ids, {"100": 1, "400": 2, "114500": 3, "101": 4, "401": 5})
            clip = matches["Assets/AnimationClip/Open.anim"]
            self.assertEqual((clip.status, clip.original_guid), ("matched", None))

    @unittest.skipUnless(importlib.util.find_spec("UnityPy"), "UnityPy class names are needed for component classes")
    def test_unmatched_content_is_reported_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = export(Path(tmp))
            clip = next(m for m in match_export(project, model(clip_name="Close"), Path(tmp)) if m.kind == "AnimationClip")
            self.assertEqual((clip.status, clip.object_id), ("unmatched", None))

    @unittest.skipUnless(importlib.util.find_spec("UnityPy"), "UnityPy class names are needed for component classes")
    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow decodes the texture pixels")
    def test_identical_pixels_resolve_only_through_a_unique_same_named_candidate(self) -> None:
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = export(root)
            (project / "Assets" / "Texture2D").mkdir()
            Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(project / "Assets" / "Texture2D" / "icon.png")
            (project / "Assets" / "Texture2D" / "icon.png.meta").write_text("guid: " + "c" * 32 + "\nTextureImporter:\n")
            (root / "shared").mkdir()
            for name in ("a.png", "b.png"):
                Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(root / "shared" / name)

            def textures(second_name: str) -> object:
                base = model()
                extra = (ObjectRecord(identity("Data/t1", "t1", 1), "Data/t1", "t1", 1, "Texture2D", "icon", {}, "complete", (), "shared/a.png"),
                         ObjectRecord(identity("Data/t2", "t2", 1), "Data/t2", "t2", 1, "Texture2D", second_name, {}, "complete", (), "shared/b.png"))
                files = base.files + (FileRecord("Data/t1", "t1", ()), FileRecord("Data/t2", "t2", ()))
                return resolve(files, base.objects + extra, ())

            unique = next(m for m in match_export(project, textures("icon_old"), root) if m.kind == "Texture2D")
            self.assertEqual((unique.status, unique.object_id), ("matched", identity("Data/t1", "t1", 1)))
            self.assertIn("same-named", unique.evidence)
            same = next(m for m in match_export(project, textures("icon"), root) if m.kind == "Texture2D")
            self.assertEqual(same.status, "ambiguous")

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow decodes the texture pixels")
    def test_near_pixels_tolerates_decoder_rounding_and_transparent_rgb_only(self) -> None:
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / f"{i}.png" for i in range(4)]
            Image.new("RGBA", (2, 1), (100, 100, 100, 255)).save(paths[0])
            Image.new("RGBA", (2, 1), (101, 99, 100, 255)).save(paths[1])
            Image.new("RGBA", (2, 1), (110, 100, 100, 255)).save(paths[2])
            Image.new("RGBA", (2, 1), (0, 0, 0, 0)).save(paths[3])
            self.assertTrue(near_pixels(paths[0], paths[1]))
            self.assertFalse(near_pixels(paths[0], paths[2]))
            Image.new("RGBA", (2, 1), (9, 9, 9, 0)).save(paths[2])
            self.assertTrue(near_pixels(paths[2], paths[3]), "RGB under alpha 0 carries no image content")

    def test_rewrite_replaces_guids_everywhere_and_links_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = export(Path(tmp))
            (project / "Assets" / "image.png").write_bytes(b"\x89PNG\0binary")
            target = Path(tmp) / "out"
            (project / "Assets" / "Big.unity").write_bytes(b"line\r\nm_Prefab: {guid: " + b"a" * 32 + b"}\r\n" + b"x" * (70 << 20))
            rewrite_project(project, target, {"a" * 32: GUID_FILE})
            big = (target / "Assets" / "Big.unity").read_bytes()
            self.assertIn(GUID_FILE.encode(), big[:200], "files above any size limit are rewritten too")
            self.assertIn(b"\r\n", big[:200], "line endings are kept")
            self.assertIn(GUID_FILE, (target / "Assets" / "Scene.unity").read_text())
            self.assertIn("guid: " + GUID_FILE, (target / "Assets" / "Prefabs" / "Fx_Hit01.prefab.meta").read_text())
            self.assertNotEqual((target / "Assets" / "image.png").stat().st_ino, (project / "Assets" / "image.png").stat().st_ino,
                                "copies, not links: editing the copy must not reach the export")
            self.assertEqual((target / "Assets" / "image.png").read_bytes(), b"\x89PNG\0binary")
            self.assertIn("a" * 32, (project / "Assets" / "Scene.unity").read_text(), "the source export must stay untouched")

    def test_catalog_identities_follow_guid_container_keys(self) -> None:
        guid = "c" * 32
        objects = (obj("CAB", 1, "AssetBundle", {"m_Container": [[guid, {"asset": ptr(2)}], ["other", {"asset": ptr(3)}]]}),
                   obj("CAB", 2, "AnimationClip", {"m_Name": "Open"}), obj("CAB", 3, "Material", {"m_Name": "M"}))
        found = catalog_identities(resolve((FileRecord("Data/CAB", "CAB", ()),), objects, ()),
                                   {guid: "Assets/_Art/Anim/Open.anim"})
        self.assertEqual(found, {objects[1].id: (guid, "Assets/_Art/Anim/Open.anim")})

    def test_catalog_paths_keep_the_exported_extension_and_skip_conflicts(self) -> None:
        def match(path: str, oid: str) -> Match:
            return Match(path, "prefab", "a" * 32, "matched", oid, None, "hierarchy fingerprint", {})
        matches = [match("Assets/Prefabs/Hero.prefab", "hero"), match("Assets/Prefabs/A.prefab", "a"),
                   match("Assets/Prefabs/B.prefab", "b"), match("Assets/Prefabs/C.prefab", "c")]
        identities = {"hero": ("1" * 32, "Assets/_Art/Roles/Hero.FBX"), "a": ("2" * 32, "Assets/_Art/X.prefab"),
                      "b": ("3" * 32, "Assets/_Art/X.prefab"), "c": ("4" * 32, "Assets/Keep/Stay.prefab")}
        files = {m.path for m in matches} | {"Assets/Keep/Stay.prefab"}
        updated, conflicts = apply_catalog(matches, identities, files)
        self.assertEqual((updated[0].original_path, updated[0].original_guid), ("Assets/_Art/Roles/Hero.prefab", "1" * 32))
        self.assertEqual([m.original_path for m in updated[1:]], [None, None, None])
        self.assertEqual(conflicts, 3)

    def test_model_sub_assets_follow_their_main_prefab(self) -> None:
        def match(path: str, kind: str, oid: str) -> Match:
            return Match(path, kind, "a" * 32, "matched", oid, None, "evidence", {})
        matches = [match("Assets/PrefabInstance/Snake.prefab", "prefab", "go"),
                   match("Assets/Mesh/Snake_Body.asset", "Mesh", "mesh"),
                   match("Assets/AnimationClip/Die.anim", "AnimationClip", "clip")]
        source = ("5" * 32, "Assets/_Art/Monster/Snake/Snake@Die.FBX")
        updated, conflicts = apply_catalog(matches, {"go": source, "mesh": source, "clip": source}, {m.path for m in matches})
        self.assertEqual(conflicts, 0)
        self.assertEqual((updated[0].original_path, updated[0].original_guid), ("Assets/_Art/Monster/Snake/Snake@Die.prefab", "5" * 32))
        self.assertEqual((updated[1].original_path, updated[1].original_guid), ("Assets/_Art/Monster/Snake/Snake@Die/Snake_Body.asset", None))
        self.assertEqual(updated[2].original_path, "Assets/_Art/Monster/Snake/Snake@Die/Die.anim")

    def test_rewrite_moves_an_asset_with_its_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = export(Path(tmp))
            target = Path(tmp) / "out"
            rewrite_project(project, target, {"a" * 32: "1" * 32},
                            {"Assets/Prefabs/Fx_Hit01.prefab": "Assets/_Resources/Effect/Fx_Hit01.prefab"})
            moved = target / "Assets" / "_Resources" / "Effect"
            self.assertTrue((moved / "Fx_Hit01.prefab").is_file())
            self.assertIn("guid: " + "1" * 32, (moved / "Fx_Hit01.prefab.meta").read_text())
            self.assertFalse((target / "Assets" / "Prefabs" / "Fx_Hit01.prefab").exists())
            self.assertIn("1" * 32, (target / "Assets" / "Scene.unity").read_text(), "references follow the GUID, not the path")

    def test_text_assets_and_scriptable_objects_are_matched(self) -> None:
        so = obj("CAB", 1, "MonoBehaviour", {"m_Name": "MapData001", "m_GameObject": ptr(0), "m_Script": ptr(9),
                                             "width": 3, "tiles": []})
        text = ObjectRecord(identity("Data/CAB", "CAB", 2), "Data/CAB", "CAB", 2, "TextAsset", "Chapter", {"m_Name": "Chapter"},
                            "complete", (), "shared/textassets/Chapter.bytes")
        game = resolve((FileRecord("Data/CAB", "CAB", ()),), (so, text), ())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shared" / "textassets").mkdir(parents=True)
            (root / "shared" / "textassets" / "Chapter.bytes").write_bytes(b"\x01\x02config")
            project = root / "ExportedProject"
            assets = project / "Assets"
            assets.mkdir(parents=True)
            (assets / "Chapter.bytes").write_bytes(b"\x01\x02config")
            (assets / "Chapter.bytes.meta").write_text("guid: " + "1" * 32 + "\nTextScriptImporter:\n")
            (assets / "MapData001.asset").write_text("%YAML 1.1\n--- !u!114 &11400000\nMonoBehaviour:\n  m_GameObject: {fileID: 0}\n"
                                                     "  m_Name: MapData001\n  width: 3\n  tiles: []\n")
            (assets / "MapData001.asset.meta").write_text("guid: " + "2" * 32 + "\n")
            matches = {m.path: m for m in match_export(project, game, root)}
        self.assertEqual((matches["Assets/Chapter.bytes"].status, matches["Assets/Chapter.bytes"].object_id), ("matched", text.id))
        self.assertEqual((matches["Assets/MapData001.asset"].status, matches["Assets/MapData001.asset"].object_id), ("matched", so.id))

    def test_identical_copies_of_a_catalog_asset_are_merged(self) -> None:
        def ambiguous(path: str, guid: str, candidates: tuple[str, ...], kind: str = "Texture2D") -> Match:
            return Match(path, kind, guid, "ambiguous", None, None, "decoded pixel hash; 2 candidates", {}, candidates=candidates)
        matches = [ambiguous("Assets/Texture2D/Glow_011.png", "1" * 32, ("bundle", "shared")),
                   ambiguous("Assets/Texture2D/Glow_011_0.png", "2" * 32, ("bundle", "shared")),
                   ambiguous("Assets/Texture2D/Other.png", "3" * 32, ("x", "y")),
                   ambiguous("Assets/Prefab/P.prefab", "4" * 32, ("bundle", "shared"), kind="prefab")]
        merged = merge_catalog_duplicates(matches, {"bundle": ("9" * 32, "Assets/_Art/PublicFX/Textures/Glow_011.png")})
        self.assertEqual((merged[0].status, merged[0].object_id), ("matched", "bundle"))
        self.assertEqual((merged[1].status, merged[1].merged_into), ("duplicate", "Assets/Texture2D/Glow_011.png"))
        self.assertEqual(merged[2].status, "ambiguous", "no candidate has a catalog identity")
        self.assertEqual(merged[3].status, "ambiguous", "prefab file IDs differ between copies; never merged")

    def test_identical_exports_pair_with_several_catalog_paths(self) -> None:
        def ambiguous(path: str, kind: str = "AnimationClip") -> Match:
            return Match(path, kind, path[-1] * 32, "ambiguous", None, None, "clip print; 3 candidates", {},
                         candidates=("a", "b", "c"))
        matches = [ambiguous("Assets/AnimationClip/Idle_2"), ambiguous("Assets/AnimationClip/Idle_1"),
                   ambiguous("Assets/AnimationClip/Idle_3")]
        identities = {"a": ("1" * 32, "Assets/_Art/B/Idle.anim"), "b": ("2" * 32, "Assets/_Art/A/Idle.anim")}
        merged = merge_catalog_duplicates(matches, identities)
        by_path = {m.path: m for m in merged}
        self.assertEqual(by_path["Assets/AnimationClip/Idle_1"].object_id, "b", "first export takes the first catalog path")
        self.assertEqual(by_path["Assets/AnimationClip/Idle_2"].object_id, "a")
        self.assertEqual((by_path["Assets/AnimationClip/Idle_3"].status, by_path["Assets/AnimationClip/Idle_3"].merged_into),
                         ("duplicate", "Assets/AnimationClip/Idle_1"))
        self.assertIn("paired in sorted order", by_path["Assets/AnimationClip/Idle_1"].evidence)

    def test_rewrite_skips_merged_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = export(Path(tmp))
            target = Path(tmp) / "out"
            rewrite_project(project, target, {"b" * 32: GUID_FILE}, skip=frozenset({"Assets/AnimationClip/Open.anim"}))
            self.assertFalse((target / "Assets" / "AnimationClip" / "Open.anim").exists())
            self.assertFalse((target / "Assets" / "AnimationClip" / "Open.anim.meta").exists())

    def test_unplaced_exports_leave_the_project_root(self) -> None:
        files = {"Assets/Hero.prefab", "Assets/Material/Glow.mat", "Assets/Plugins/Game.dll", "Assets/_Art/X.png",
                 "Assets/Scene/Main.unity", "Assets/Art/Y.png", "Assets/Done.anim"}
        moves = unmapped_moves(files, {"Assets/Done.anim": "Assets/_Art/Done.anim"}, {"Art", "_Art"})
        self.assertEqual(moves["Assets/Hero.prefab"], "Assets/_Unmapped/AssetRipper/Prefab/Hero.prefab")
        self.assertEqual(moves["Assets/Material/Glow.mat"], "Assets/_Unmapped/AssetRipper/Material/Glow.mat")
        self.assertEqual(moves["Assets/Material"], "Assets/_Unmapped/AssetRipper/Material")
        for kept in ("Assets/Plugins/Game.dll", "Assets/_Art/X.png", "Assets/Scene/Main.unity", "Assets/Art/Y.png", "Assets/Done.anim"):
            self.assertNotIn(kept, moves)


if __name__ == "__main__":
    unittest.main()
