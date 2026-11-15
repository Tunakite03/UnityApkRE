import tempfile
import unittest
from pathlib import Path

from uare.asset_origins import COPY_EVIDENCE, SPRITE_KEY_EVIDENCE, DerivedOrigins, derive_origins, sprite_source_guid
from uare.game_model import FileRecord, Json, ObjectRecord, identity, outgoing, resolve

PLAYER = "Data/sharedassets0.assets"
BUNDLE = "assets/ui.bundle"
OTHER = "assets/fx.bundle"
# A render data key and the GUID Unity spells for it (each byte low nibble first).
KEY_WORDS = (2209765458, 1281618540, 325020588, 2960557074)
KEY_GUID = "25856b38c62f36c4cab6f5312148670b"


def ptr(file_id: int, path_id: int) -> dict[str, Json]:
    return {"m_FileID": file_id, "m_PathID": path_id}


def obj(source: str, pid: int, cls: str, data: dict[str, Json], output: str | None = None) -> ObjectRecord:
    file = source.rsplit("/", 1)[-1]
    return ObjectRecord(identity(source, file, pid), source, file, pid, cls, str(data.get("m_Name", "")), data,
                        "complete", (), output)


def render_key(words: tuple[int, ...] = KEY_WORDS) -> list[Json]:
    return [{f"data[{i}]": word for i, word in enumerate(words)}, 21300000]


def derive(objects: list[ObjectRecord], recorded: dict[str, str], root: Path,
           catalog: dict[str, str] | None = None) -> DerivedOrigins:
    files = tuple(FileRecord(source, source.rsplit("/", 1)[-1], ("Resources/unity_builtin_extra",))
                  for source in sorted({o.source for o in objects}))
    model = resolve(files, tuple(objects), ())
    return derive_origins(model, {o.id: o for o in model.objects}, outgoing(model), recorded, catalog or {}, root)


def write(root: Path, relative: str, content: bytes) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return relative


class AssetOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_render_data_key_spells_the_source_texture_guid(self) -> None:
        sprite = obj(PLAYER, 1, "Sprite", {"m_Name": "logo", "m_RenderDataKey": render_key()})
        self.assertEqual(sprite_source_guid(sprite), KEY_GUID)
        self.assertIsNone(sprite_source_guid(obj(PLAYER, 2, "Sprite", {"m_RenderDataKey": render_key((0, 0, 0, 0))})))
        self.assertIsNone(sprite_source_guid(obj(PLAYER, 3, "Sprite", {"m_RenderDataKey": [{"data[0]": -1}, 0]})))
        self.assertIsNone(sprite_source_guid(obj(PLAYER, 4, "Sprite", {"m_Name": "no key"})))

    def test_sprite_and_its_own_texture_take_the_catalog_path_of_the_source_guid(self) -> None:
        sprite = obj(PLAYER, 1, "Sprite", {"m_Name": "logo", "m_RenderDataKey": render_key(), "m_AtlasTags": [],
                                           "m_SpriteAtlas": ptr(0, 0), "m_RD": {"texture": ptr(0, 2)}})
        texture = obj(PLAYER, 2, "Texture2D", {"m_Name": "logo"})
        packed = obj(PLAYER, 3, "Sprite", {"m_Name": "icon", "m_RenderDataKey": render_key(), "m_AtlasTags": ["common"],
                                           "m_SpriteAtlas": ptr(0, 5), "m_RD": {"texture": ptr(0, 4)}})
        page = obj(PLAYER, 4, "Texture2D", {"m_Name": "sactx-0-common"})
        atlas = obj(PLAYER, 5, "SpriteAtlas", {"m_Name": "common"})
        result = derive([sprite, texture, packed, page, atlas], {}, self.root, {KEY_GUID: "Assets/Art/logo.png"})
        self.assertEqual({oid: (o.path, o.evidence, o.copy_of) for oid, o in result.origins.items()},
                         {oid: ("Assets/Art/logo.png", SPRITE_KEY_EVIDENCE, None) for oid in (sprite.id, texture.id, packed.id)})

    def test_identical_copy_takes_the_recorded_path_and_references_compare_by_target(self) -> None:
        # The same material in player data and in a bundle: path IDs and file indexes differ, the targets do not.
        player = [obj(PLAYER, 4, "Material", {"m_Name": "Glow", "m_Shader": ptr(1, 46), "m_Color": {"r": 1.0},
                                              "m_TexEnvs": [["_MainTex", {"m_Texture": ptr(0, 9)}]]}),
                  obj(PLAYER, 9, "Texture2D", {"m_Name": "Glow_011"})]
        bundle = [obj(BUNDLE, -77, "Material", {"m_Name": "Glow", "m_Shader": ptr(1, 46), "m_Color": {"r": 1.0},
                                                "m_TexEnvs": [["_MainTex", {"m_Texture": ptr(0, 123)}]]}),
                  obj(BUNDLE, 123, "Texture2D", {"m_Name": "Glow_011"})]
        recolored = obj(OTHER, 5, "Material", {"m_Name": "Glow", "m_Shader": ptr(1, 46), "m_Color": {"r": 0.5},
                                               "m_TexEnvs": [["_MainTex", {"m_Texture": ptr(0, 0)}]]})
        recorded = {bundle[0].id: "Assets/FX/Glow.mat", recolored.id: "Assets/Other/Glow.mat"}
        result = derive(player + bundle + [recolored], recorded, self.root)
        origin = result.origins[player[0].id]
        self.assertEqual((origin.path, origin.evidence, origin.copy_of), ("Assets/FX/Glow.mat", COPY_EVIDENCE, bundle[0].id))
        # The textures share a name too, but neither has a recorded path to pass on.
        self.assertEqual((set(result.origins), result.ambiguous), ({player[0].id}, 0))

    def test_same_name_with_different_content_or_another_shader_is_not_a_copy(self) -> None:
        player = obj(PLAYER, 4, "Material", {"m_Name": "Glow", "m_Shader": ptr(1, 46), "m_Color": {"r": 1.0}})
        other_shader = obj(BUNDLE, 7, "Material", {"m_Name": "Glow", "m_Shader": ptr(1, 47), "m_Color": {"r": 1.0}})
        other_value = obj(OTHER, 7, "Material", {"m_Name": "Glow", "m_Shader": ptr(1, 46), "m_Color": {"r": 0.9}})
        recorded = {other_shader.id: "Assets/A/Glow.mat", other_value.id: "Assets/B/Glow.mat"}
        self.assertEqual(derive([player, other_shader, other_value], recorded, self.root).origins, {})

    def test_reference_holders_are_told_apart_by_what_they_reference(self) -> None:
        # Spine data assets of different characters: same name, same fields, same target names at every level.
        def spine(source: str, skeleton: str) -> list[ObjectRecord]:
            return [obj(source, 1, "MonoBehaviour", {"m_Name": "skeleton_SkeletonData", "scale": 0.01, "atlas": ptr(0, 2)}),
                    obj(source, 2, "MonoBehaviour", {"m_Name": "skeleton_Atlas", "atlasFile": ptr(0, 3)}),
                    obj(source, 3, "TextAsset", {"m_Name": "skeleton.atlas", "m_Script": skeleton})]
        player, hero, boss = spine(PLAYER, "hero bones"), spine(BUNDLE, "hero bones"), spine(OTHER, "boss bones")
        result = derive(player + hero + boss, {boss[0].id: "Assets/Spine/Boss/skeleton_SkeletonData.asset"}, self.root)
        self.assertEqual(result.origins, {})
        result = derive(player + hero + boss, {hero[0].id: "Assets/Spine/Hero/skeleton_SkeletonData.asset"}, self.root)
        self.assertEqual({oid: o.copy_of for oid, o in result.origins.items()}, {player[0].id: hero[0].id})

    def test_asset_referencing_a_payload_that_was_not_exported_is_not_matched(self) -> None:
        def material(source: str) -> list[ObjectRecord]:
            return [obj(source, 1, "Material", {"m_Name": "Glow", "m_Texture": ptr(0, 2)}),
                    obj(source, 2, "Texture2D", {"m_Name": "Glow", "image data": {"$binary_base64": "AAAA"}})]
        player, bundle = material(PLAYER), material(BUNDLE)
        self.assertEqual(derive(player + bundle, {bundle[0].id: "Assets/FX/Glow.mat"}, self.root).origins, {})

    def test_font_file_is_decisive_without_its_generated_texture(self) -> None:
        def font(source: str, relative: str, content: bytes) -> list[ObjectRecord]:
            return [obj(source, 1, "Font", {"m_Name": "Heavy", "m_Texture": ptr(0, 2)}, write(self.root, relative, content)),
                    obj(source, 2, "Texture2D", {"m_Name": "Font Texture", "image data": {"$binary_base64": ""}})]
        player = font(PLAYER, "shared/fonts/_serialized/Heavy.ttf", b"glyphs")
        same, other = font(BUNDLE, "shared/fonts/ui/Heavy.ttf", b"glyphs"), font(OTHER, "shared/fonts/fx/Heavy.ttf", b"bold glyphs")
        recorded = {same[0].id: "Assets/Fonts/Heavy.ttf", other[0].id: "Assets/Other/Heavy.ttf"}
        result = derive(player + same + other, recorded, self.root)
        self.assertEqual({oid: (o.path, o.copy_of) for oid, o in result.origins.items()},
                         {player[0].id: ("Assets/Fonts/Heavy.ttf", same[0].id)})

    def test_copies_recorded_under_several_paths_are_ambiguous(self) -> None:
        data: dict[str, Json] = {"m_Name": "Button", "m_Speed": 1.0}
        player = obj(PLAYER, 1, "AnimatorController", dict(data))
        first, second = obj(BUNDLE, 2, "AnimatorController", dict(data)), obj(OTHER, 3, "AnimatorController", dict(data))
        recorded = {first.id: "Assets/Animation/Button.controller", second.id: "Assets/Animators/Button.controller"}
        result = derive([player, first, second], recorded, self.root)
        self.assertEqual((result.origins, result.ambiguous), ({}, 1))

    def test_payload_is_compared_through_the_exported_file(self) -> None:
        # Player data keeps the pixels inline, a bundle streams them; only the exported files can be compared.
        fields: dict[str, Json] = {"m_Name": "Logo", "m_Width": 4, "m_Height": 4, "m_TextureFormat": 50}
        inline = {**fields, "image data": {"$binary_base64": "AAAA"}, "m_StreamData": {"offset": 0, "size": 0, "path": ""}}
        streamed = {**fields, "image data": {"$binary_base64": ""},
                    "m_StreamData": {"offset": 64, "size": 3, "path": "archive:/CAB-1/CAB-1.resS"}}
        player = obj(PLAYER, 1, "Texture2D", inline, write(self.root, "shared/textures/_serialized/Logo.png", b"pixels"))
        same = obj(BUNDLE, 2, "Texture2D", streamed, write(self.root, "shared/textures/ui/Logo.png", b"pixels"))
        different = obj(OTHER, 3, "Texture2D", streamed, write(self.root, "shared/textures/fx/Logo.png", b"other pixels"))
        unexported = obj(PLAYER, 4, "Texture2D", inline)
        recorded = {same.id: "Assets/Art/Logo.png", different.id: "Assets/FX/Logo.png"}
        result = derive([player, same, different, unexported], recorded, self.root)
        self.assertEqual({oid: (o.path, o.copy_of) for oid, o in result.origins.items()},
                         {player.id: ("Assets/Art/Logo.png", same.id)})
        self.assertEqual(result.ambiguous, 0)

    def test_components_and_hierarchy_objects_are_not_matched(self) -> None:
        player = obj(PLAYER, 1, "MonoBehaviour", {"m_Name": "Config", "m_GameObject": ptr(0, 0), "value": 1})
        bundle = obj(BUNDLE, 2, "MonoBehaviour", {"m_Name": "Config", "m_GameObject": ptr(0, 0), "value": 1})
        root = obj(PLAYER, 3, "GameObject", {"m_Name": "Root"})
        twin = obj(BUNDLE, 4, "GameObject", {"m_Name": "Root"})
        recorded = {bundle.id: "Assets/Config.asset", twin.id: "Assets/Root.prefab"}
        self.assertEqual(derive([player, bundle, root, twin], recorded, self.root).origins, {})


if __name__ == "__main__":
    unittest.main()
