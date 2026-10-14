import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from uare.game_model import FileRecord, GameModel, Json, ObjectRecord, identity, resolve, save_model
from uare.scene_spec import SceneSpecError, build_specs

SCENE = "Data/level0"
BUNDLE = "Data/shared.bundle"


def ptr(file_id: int, path_id: int) -> dict[str, Json]:
    return {"m_FileID": file_id, "m_PathID": path_id}


def obj(source: str, file: str, pid: int, cls: str, data: dict[str, Json], output: str | None = None) -> ObjectRecord:
    return ObjectRecord(identity(source, file, pid), source, file, pid, cls,
                        str(data.get("m_Name", cls)), data, "complete", (), output)


def scene_objects(child_father: int = 2) -> list[ObjectRecord]:
    return [
        obj(SCENE, "level0", 1, "GameObject", {"m_Name": "Root", "m_IsActive": 1, "m_Layer": 0, "m_TagString": "Untagged",
            "m_Component": [{"component": ptr(0, 2)}]}),
        obj(SCENE, "level0", 2, "Transform", {"m_GameObject": ptr(0, 1), "m_Father": ptr(0, 0),
            "m_Children": [ptr(0, 4)], "m_LocalPosition": {"x": 1.5, "y": 2, "z": 0}}),
        obj(SCENE, "level0", 3, "GameObject", {"m_Name": "Hero", "m_IsActive": 0, "m_Layer": 8, "m_TagString": "Player",
            "m_Component": [{"component": ptr(0, 4)}, {"component": ptr(0, 5)}, {"component": ptr(0, 6)}]}),
        obj(SCENE, "level0", 4, "Transform", {"m_GameObject": ptr(0, 3), "m_Father": ptr(0, child_father),
            "m_Children": []}),
        obj(SCENE, "level0", 5, "SpriteRenderer", {"m_GameObject": ptr(0, 3), "m_Enabled": True,
            "m_Sprite": ptr(1, 20), "m_Materials": [ptr(1, 30)], "m_SortingLayerID": 7}),
        obj(SCENE, "level0", 6, "MonoBehaviour", {"m_GameObject": ptr(0, 3), "m_Script": ptr(1, 10), "m_Name": "",
            "speed": 3.5, "target": ptr(0, 1), "lookup": [["k", ptr(0, 4)]], "broken": ptr(1, 999)}),
    ]


def shared_objects() -> list[ObjectRecord]:
    return [
        obj(BUNDLE, "CAB", 10, "MonoScript", {"m_ClassName": "HeroMover", "m_Namespace": "Game",
            "m_AssemblyName": "GamePlay.dll"}),
        obj(BUNDLE, "CAB", 20, "Sprite", {"m_Name": "hero", "m_PixelsToUnits": 64, "m_Pivot": {"x": 0.5, "y": 0},
            "m_Border": {"x": 1, "y": 2, "z": 3, "w": 4}}, "shared/sprites/scenes/hero.png"),
        obj(BUNDLE, "CAB", 21, "Texture2D", {"m_Name": "atlas", "m_MipCount": 1, "m_IsReadable": False, "m_TextureFormat": 1,
            "m_TextureSettings": {"m_FilterMode": 0, "m_WrapU": 1, "m_WrapV": 1}}, "shared/textures/scenes/atlas.png"),
        obj(BUNDLE, "CAB", 30, "Material", {"m_Name": "HeroMat", "m_Shader": ptr(0, 40), "m_ValidKeywords": ["PIXELSNAP_ON"],
            "m_CustomRenderQueue": 3000, "m_SavedProperties": {
            "m_TexEnvs": [["_MainTex", {"m_Texture": ptr(0, 21), "m_Scale": {"x": 1, "y": 1}, "m_Offset": {"x": 0, "y": 0}}]],
            "m_Floats": [["_Cutoff", 0.5]],
            "m_Colors": [["_Color", {"r": 0.5, "g": 1, "b": 1, "a": 0.25}]]}}, "shared/materials/scenes/HeroMat.json"),
        obj(BUNDLE, "CAB", 40, "Shader", {"m_Name": "", "m_ParsedForm": {"m_Name": "Sprites/Default"}},
            "shared/shaders/scenes/Sprites-Default.json"),
        obj("Data/globalgamemanagers", "globalgamemanagers", 50, "TagManager", {
            "tags": ["Player"], "layers": ["Default", "", "", "", "", "UI", "", "", "Hero"],
            "m_SortingLayers": [{"name": "Default", "uniqueID": 0}, {"name": "Actors", "uniqueID": 7}]}),
    ]


def model(objects: list[ObjectRecord]) -> GameModel:
    return resolve((FileRecord(SCENE, "level0", ("archive:/CAB/CAB",)), FileRecord(BUNDLE, "CAB", ()),
                    FileRecord("Data/globalgamemanagers", "globalgamemanagers", ())), tuple(objects), ())


def package(root: Path, game: GameModel) -> Path:
    assets = root / "extracted-assets"
    (assets / "scenes" / "Main").mkdir(parents=True)
    save_model(assets / "game_model.json", game)
    (assets / "scenes" / "Main" / "scene.json").write_text(json.dumps({
        "scope_id": "x-scene", "kind": "scene", "name": "Assets/Scenes/Main.unity",
        "source": SCENE, "serialized_file": "level0", "roots": []}), encoding="utf-8")
    return assets


def props(component: dict[str, Json]) -> dict[str, dict[str, Json]]:
    return {str(row["path"]): row for row in component["props"]}


class SceneSpecTests(unittest.TestCase):
    def build(self, objects: list[ObjectRecord]) -> tuple[dict[str, Json], object]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = build_specs(package(root, model(objects)), root / "specs")
            spec = json.loads((root / "specs" / "Main.json").read_text(encoding="utf-8"))
        return spec, result

    def test_hierarchy_is_depth_first_with_parent_sibling_and_object_state(self) -> None:
        spec, result = self.build(scene_objects() + shared_objects())
        rows = spec["gameObjects"]
        self.assertEqual([r["name"] for r in rows], ["Root", "Hero"])
        self.assertEqual(rows[1]["parentId"], rows[0]["id"])
        self.assertEqual((rows[1]["active"], rows[1]["layer"], rows[1]["tag"]), (False, 8, "Player"))
        self.assertEqual([c["className"] for c in rows[1]["components"]],
                         ["Transform", "SpriteRenderer", "MonoBehaviour"])
        self.assertEqual(spec["originalPath"], "Assets/Scenes/Main.unity")
        self.assertEqual((result.scenes, result.game_objects, result.components), (("Main",), 2, 4))

    def test_fields_flatten_to_unity_property_paths_without_structural_fields(self) -> None:
        spec, _ = self.build(scene_objects() + shared_objects())
        transform = props(spec["gameObjects"][0]["components"][0])
        self.assertEqual(transform["m_LocalPosition.x"]["number"], 1.5)
        self.assertNotIn("m_Father", transform)
        self.assertFalse(any(path.startswith("m_Children") for path in transform))
        renderer = props(spec["gameObjects"][1]["components"][1])
        self.assertEqual(renderer["m_Enabled"]["number"], 1.0)
        self.assertEqual(renderer["m_Materials.Array.size"]["number"], 1.0)
        self.assertEqual(renderer["m_SortingLayerID"]["number"], 7.0)

    def test_non_finite_floats_become_number_rows_with_their_spelling(self) -> None:
        objects = scene_objects()
        objects[1] = replace(objects[1], data={**objects[1].data, "m_LocalPosition": {
            "x": {"$float": "NaN"}, "y": {"$float": "-Infinity"}, "z": 0}})
        spec, _ = self.build(objects + shared_objects())
        transform = props(spec["gameObjects"][0]["components"][0])
        self.assertEqual((transform["m_LocalPosition.x"]["kind"], transform["m_LocalPosition.x"]["text"]), ("number", "NaN"))
        self.assertEqual(transform["m_LocalPosition.y"]["text"], "-Infinity")
        self.assertEqual((transform["m_LocalPosition.z"]["number"], transform["m_LocalPosition.z"]["text"]), (0.0, ""))
        self.assertFalse(any("$float" in path for path in transform))

    def test_non_finite_material_property_is_dropped_and_reported(self) -> None:
        shared = shared_objects()
        material = shared[3]
        props = material.data["m_SavedProperties"]
        shared[3] = replace(material, data={**material.data, "m_SavedProperties": {
            **props, "m_Colors": [["_Color", {"r": 0.5, "g": {"$float": "Infinity"}, "b": 1, "a": 1}]]}})
        spec, _ = self.build(scene_objects() + shared)
        entry = next(a for a in spec["assets"] if a["className"] == "Material")
        self.assertEqual(entry["colors"], [])
        self.assertEqual(entry["droppedProperties"], ["color _Color holds non-finite Infinity"])

    def test_references_resolve_to_scene_objects_assets_missing_or_unresolved(self) -> None:
        spec, result = self.build(scene_objects() + shared_objects())
        hero = spec["gameObjects"][1]
        renderer = props(hero["components"][1])
        self.assertEqual((renderer["m_Sprite"]["refKind"], renderer["m_Sprite"]["refTarget"]),
                         ("asset", "shared/sprites/scenes/hero.png"))
        self.assertEqual(renderer["m_Materials.Array.data[0]"]["refTarget"], "shared/materials/scenes/HeroMat.json")
        script = hero["components"][2]
        self.assertEqual((script["scriptClass"], script["scriptNamespace"], script["scriptAssembly"]),
                         ("HeroMover", "Game", "GamePlay.dll"))
        fields = props(script)
        self.assertEqual((fields["target"]["refKind"], fields["target"]["refTarget"]),
                         ("object", spec["gameObjects"][0]["id"]))
        self.assertEqual(fields["lookup.Array.data[0].first"]["text"], "k")
        self.assertEqual(fields["lookup.Array.data[0].second"]["refKind"], "object")
        self.assertEqual((fields["broken"]["refKind"], fields["broken"]["refClass"]), ("unresolved", "missing_object"))
        self.assertEqual(json.loads(script["rawJson"])["speed"], 3.5)
        self.assertEqual(result.unresolved_references, 1)

    def test_asset_table_carries_sprite_texture_and_material_settings(self) -> None:
        objects = scene_objects() + shared_objects()
        objects[objects.index(next(o for o in objects if o.path_id == 20 and o.source == BUNDLE))] = obj(
            BUNDLE, "CAB", 20, "Sprite", {"m_Name": "hero", "m_PixelsToUnits": 64, "m_Pivot": {"x": 0.5, "y": 0},
                                          "m_Border": {"x": 1, "y": 2, "z": 3, "w": 4}, "m_RD": {"texture": ptr(0, 21)},
                                          "m_Rect": {"x": 0, "y": 0, "width": 32, "height": 16}},
            "shared/sprites/scenes/hero.png")
        texture = next(o for o in objects if o.path_id == 21 and o.source == BUNDLE)
        objects[objects.index(texture)] = replace(texture, data={**texture.data, "m_Width": 32, "m_Height": 16})
        spec, _ = self.build(objects)
        assets = {a["path"]: a for a in spec["assets"]}
        self.assertNotIn("shared/sprites/scenes/hero.png", assets)
        sprite = assets["shared/textures/scenes/atlas.png#hero"]
        self.assertEqual((sprite["kind"], sprite["spriteName"], sprite["imagePath"], sprite["importPath"]),
                         ("sprite", "hero", "shared/textures/scenes/atlas.png", "shared/textures/scenes/atlas.png"))
        image = assets["shared/textures/scenes/atlas.png"]
        self.assertEqual((image["spriteMode"], image["pixelsPerUnit"]), ("single", 64.0))
        self.assertEqual((image["sprites"][0]["pivotY"], image["sprites"][0]["borderTop"], image["sprites"][0]["width"]), (0.0, 4.0, 32.0))
        self.assertEqual((image["filterMode"], image["wrapU"], image["mipmaps"], image["alphaOnly"]), (0, 1, False, True))
        renderer = props(spec["gameObjects"][1]["components"][1])
        self.assertEqual(renderer["m_Sprite"]["refTarget"], "shared/textures/scenes/atlas.png#hero")
        material = assets["shared/materials/scenes/HeroMat.json"]
        self.assertEqual((material["kind"], material["importPath"], material["shaderName"]),
                         ("material", "shared/materials/scenes/HeroMat.mat", "Sprites/Default"))
        self.assertEqual(material["textures"], [{"name": "_MainTex", "path": "shared/textures/scenes/atlas.png", "missing": "",
                                                 "scaleX": 1.0, "scaleY": 1.0, "offsetX": 0.0, "offsetY": 0.0}])
        self.assertEqual(material["colors"], [{"name": "_Color", "r": 0.5, "g": 1.0, "b": 1.0, "a": 0.25}])
        self.assertEqual((material["floats"], material["keywords"], material["renderQueue"]),
                         ([{"name": "_Cutoff", "value": 0.5}], ["PIXELSNAP_ON"], 3000))
        self.assertIn("shared/textures/scenes/atlas.png", assets)
        self.assertEqual(spec["sortingLayers"][1], {"name": "Actors", "uniqueId": 7})
        self.assertEqual(spec["layers"][8], "Hero")
        self.assertEqual(spec["rootOrderBasis"], "path_id")

    def test_sprites_cut_from_one_texture_form_a_multiple_sheet_with_unique_names(self) -> None:
        objects = scene_objects() + shared_objects()
        sprite = next(o for o in objects if o.path_id == 20 and o.source == BUNDLE)
        cut = {"m_RD": {"texture": ptr(0, 21)}, "m_PixelsToUnits": 64, "m_Rect": {"x": 0, "y": 0, "width": 8, "height": 8}}
        objects[objects.index(sprite)] = replace(sprite, data={**sprite.data, **cut})
        objects.append(obj(BUNDLE, "CAB", 22, "Sprite", {"m_Name": "hero", **cut, "m_Rect": {"x": 8, "y": 0, "width": 8, "height": 8}}))
        spec, _ = self.build(objects)
        image = next(a for a in spec["assets"] if a["path"] == "shared/textures/scenes/atlas.png")
        self.assertEqual(image["spriteMode"], "multiple")
        self.assertEqual([(s["name"], s["x"]) for s in image["sprites"]], [("hero", 0.0), ("hero_22", 8.0)])

    def test_sprite_without_an_exported_texture_keeps_its_cropped_file(self) -> None:
        spec, _ = self.build(scene_objects() + shared_objects())
        sprite = next(a for a in spec["assets"] if a["className"] == "Sprite")
        self.assertEqual((sprite["kind"], sprite["path"], sprite["pixelsPerUnit"]), ("file", "shared/sprites/scenes/hero.png", 64.0))

    def test_text_assets_keep_their_name_and_avoid_spine_importer_names(self) -> None:
        objects = scene_objects() + shared_objects()
        for pid, file in ((60, "boot.lua"), (61, "table.json"), (62, "hero.skel"), (63, "hero.atlas")):
            objects.append(obj(BUNDLE, "CAB", pid, "TextAsset", {"m_Name": file}, f"shared/text/scenes/{file}"))
        objects[5] = obj(SCENE, "level0", 6, "MonoBehaviour", {
            "m_GameObject": ptr(0, 3), "m_Script": ptr(1, 10), "a": ptr(1, 60), "b": ptr(1, 61), "c": ptr(1, 62), "d": ptr(1, 63)})
        spec, _ = self.build(objects)
        imports = {a["path"].rsplit("/", 1)[1]: a["importPath"].rsplit("/", 1)[1] for a in spec["assets"] if a["className"] == "TextAsset"}
        self.assertEqual(imports, {"boot.lua": "boot.lua.bytes", "table.json": "table.json",
                                   "hero.skel": "hero.skel.txt", "hero.atlas": "hero.atlas.bytes"})

    def test_monobehaviour_assets_become_scriptable_entries_with_their_own_fields(self) -> None:
        objects = scene_objects() + shared_objects()
        objects.append(obj(BUNDLE, "CAB", 70, "MonoScript", {"m_ClassName": "TMP_FontAsset", "m_Namespace": "TMPro",
                                                             "m_AssemblyName": "Unity.TextMeshPro.dll"}))
        objects.append(obj(BUNDLE, "CAB", 71, "MonoBehaviour", {
            "m_GameObject": ptr(0, 0), "m_Enabled": 1, "m_Script": ptr(0, 70), "m_Name": "Font SDF",
            "m_AtlasTextures": [ptr(0, 21)], "material": ptr(0, 30), "fallback": ptr(0, 71), "m_AtlasPadding": 9},
            "shared/monobehaviours/scenes/Font_SDF.json"))
        objects[5] = obj(SCENE, "level0", 6, "MonoBehaviour", {"m_GameObject": ptr(0, 3), "m_Script": ptr(1, 10),
                                                               "font": ptr(1, 71)})
        spec, _ = self.build(objects)
        fields = props(spec["gameObjects"][1]["components"][2])
        self.assertEqual((fields["font"]["refKind"], fields["font"]["refTarget"]),
                         ("asset", "shared/monobehaviours/scenes/Font_SDF.json"))
        font = {a["path"]: a for a in spec["assets"]}["shared/monobehaviours/scenes/Font_SDF.json"]
        self.assertEqual((font["kind"], font["importPath"], font["scriptNamespace"], font["scriptClass"]),
                         ("scriptable", "shared/monobehaviours/scenes/Font_SDF.asset", "TMPro", "TMP_FontAsset"))
        rows = props(font)
        self.assertNotIn("m_Enabled", rows)
        self.assertEqual(rows["m_AtlasTextures.Array.data[0]"]["refTarget"], "shared/textures/scenes/atlas.png")
        self.assertEqual(rows["material"]["refTarget"], "shared/materials/scenes/HeroMat.json")
        self.assertEqual(rows["fallback"]["refTarget"], "shared/monobehaviours/scenes/Font_SDF.json")
        self.assertEqual(rows["m_AtlasPadding"]["number"], 9.0)

    def test_scene_settings_are_flattened_and_non_hierarchy_scene_objects_are_missing(self) -> None:
        objects = scene_objects() + shared_objects()
        objects.append(obj(SCENE, "level0", 80, "Cubemap", {"m_Name": ""}))
        objects.append(obj(SCENE, "level0", 81, "RenderSettings", {"m_Fog": True, "m_FogColor": {"r": 0.5},
                                                                   "m_Sun": ptr(0, 2), "m_CustomReflection": ptr(0, 80)}))
        spec, _ = self.build(objects)
        settings = {s["className"]: props(s) for s in spec["sceneSettings"]}
        render = settings["RenderSettings"]
        self.assertEqual((render["m_Fog"]["number"], render["m_FogColor.r"]["number"]), (1.0, 0.5))
        self.assertEqual((render["m_Sun"]["refKind"], render["m_Sun"]["refClass"]), ("object", "Transform"))
        self.assertEqual((render["m_CustomReflection"]["refKind"], render["m_CustomReflection"]["refClass"]), ("missing", "Cubemap"))

    def test_parent_cycle_fails_loudly(self) -> None:
        objects = scene_objects() + shared_objects()
        objects[1] = obj(SCENE, "level0", 2, "Transform", {"m_GameObject": ptr(0, 1), "m_Father": ptr(0, 4),
                                                           "m_Children": [ptr(0, 4)]})
        objects[3] = obj(SCENE, "level0", 4, "Transform", {"m_GameObject": ptr(0, 3), "m_Father": ptr(0, 2),
                                                           "m_Children": [ptr(0, 2)]})
        with self.assertRaises(SceneSpecError):
            self.build(objects)

    def test_package_without_tag_manager_is_rejected(self) -> None:
        objects = [o for o in scene_objects() + shared_objects() if o.class_name != "TagManager"]
        with self.assertRaises(SceneSpecError):
            self.build(objects)


if __name__ == "__main__":
    unittest.main()
