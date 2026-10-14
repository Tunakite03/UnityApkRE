import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from uare.assetripper_remap import (_sole_asset, assetripper_fingerprint, documents, match_export, near_pixels,
                                    prefab_tree, rewrite_project, scalar)
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


if __name__ == "__main__":
    unittest.main()
