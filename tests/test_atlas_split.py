import importlib.util
import tempfile
import unittest
from pathlib import Path

from uare.assetripper_remap import catalog_scene_moves
from uare.atlas_split import UNMAPPED_FOLDER, split_atlas_sprites

PAGE = "sactx-0-8x8-RGBA32-UI_Common-1a2b3c4d"
PAGE_GUID = "a" * 32
SPRITE_GUID = "b" * 32
SOURCE_GUID = "c" * 32
PAGE_META = """fileFormatVersion: 2
guid: {guid}
TextureImporter:
  spriteMode: 2
  alignment: 0
  spritePivot: {{x: 0.5, y: 0.5}}
  spritePixelsToUnits: 86.6
  spriteBorder: {{x: 0, y: 0, z: 0, w: 0}}
  textureType: 8
"""


def sprite_asset(name: str, key: str, settings: int = 67) -> str:
    return f"""%YAML 1.1
--- !u!213 &21300000
Sprite:
  m_Name: {name}
  m_Rect:
    serializedVersion: 2
    x: 10
    y: 20
    width: 4
    height: 2
  m_Offset: {{x: 0, y: 0}}
  m_Border: {{x: 1, y: 0, z: 1, w: 0}}
  m_PixelsToUnits: 64
  m_Pivot: {{x: 0.25, y: 0.5}}
  m_RenderDataKey:
    {key}
  m_RD:
    serializedVersion: 3
    texture: {{fileID: 2800000, guid: {PAGE_GUID}, type: 3}}
    textureRect:
      serializedVersion: 2
      x: 2
      y: 1
      width: 4
      height: 2
    textureRectOffset: {{x: 0, y: 0}}
    atlasRectOffset: {{x: 0, y: 0}}
    settingsRaw: {settings}
  m_AtlasRD:
    serializedVersion: 3
"""


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow crops the atlas pages")
class AtlasSplitTests(unittest.TestCase):
    def project(self, root: Path, key: str) -> Path:
        from PIL import Image
        assets = root / "Assets"
        (assets / "Texture2D").mkdir(parents=True)
        (assets / "Sprite").mkdir()
        page = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        # Unity rect x=2..6, y=1..3 from the bottom is image rows 5..7 from the top.
        for x in range(2, 6):
            for y in range(5, 7):
                page.putpixel((x, y), (255, 0, 0, 255))
        page.save(assets / "Texture2D" / f"{PAGE}.png")
        (assets / "Texture2D" / f"{PAGE}.png.meta").write_text(PAGE_META.format(guid=PAGE_GUID))
        (assets / "Sprite" / "Icon.asset").write_text(sprite_asset("Icon", key))
        (assets / "Sprite" / "Icon.asset.meta").write_text(f"fileFormatVersion: 2\nguid: {SPRITE_GUID}\n")
        (assets / "Panel.prefab").write_text(f"  m_Sprite: {{fileID: 21300000, guid: {SPRITE_GUID}, type: 2}}\n")
        return root

    def test_sprite_becomes_its_own_image_and_the_page_goes_away(self) -> None:
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(Path(tmp), f"{SOURCE_GUID}: 21300045")  # a sheet sprite: keeps the Sprite asset GUID
            result = split_atlas_sprites(root)
            image = root / UNMAPPED_FOLDER / "UI_Common" / "Icon.png"
            with Image.open(image) as cropped:
                self.assertEqual(cropped.size, (4, 2))
                self.assertEqual(cropped.convert("RGBA").getpixel((0, 0)), (255, 0, 0, 255))
            meta = image.with_name("Icon.png.meta").read_text()
            self.assertIn(f"guid: {SPRITE_GUID}", meta)
            self.assertIn("spriteMode: 1", meta)
            self.assertIn("spritePivot: {x: 0.25, y: 0.5}", meta)
            self.assertIn("spritePixelsToUnits: 64", meta)
            self.assertIn("spriteBorder: {x: 1, y: 0, z: 1, w: 0}", meta)
            self.assertIn(f"guid: {SPRITE_GUID}, type: 3", (root / "Assets" / "Panel.prefab").read_text())
            self.assertFalse((root / "Assets" / "Sprite" / "Icon.asset").exists())
            self.assertFalse((root / "Assets" / "Texture2D" / f"{PAGE}.png").exists())
            self.assertEqual((result.sprites_split, result.original_guids, result.pages_removed), (1, 0, 1))

    def test_single_sprite_source_takes_its_original_guid_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(Path(tmp), f"{SOURCE_GUID}: 21300000")
            result = split_atlas_sprites(root, {SOURCE_GUID: "Assets/_Art/Textures/UI/Icon.psd"})
            image = root / "Assets" / "_Art" / "Textures" / "UI" / "Icon.png"
            self.assertTrue(image.is_file())
            self.assertIn(f"guid: {SOURCE_GUID}", image.with_name("Icon.png.meta").read_text())
            self.assertIn(f"guid: {SOURCE_GUID}, type: 3", (root / "Assets" / "Panel.prefab").read_text())
            self.assertEqual((result.original_guids, result.original_paths), (1, 1))

    def test_rotated_sprite_and_its_page_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(Path(tmp), f"{SOURCE_GUID}: 21300000")
            sprite = root / "Assets" / "Sprite" / "Icon.asset"
            sprite.write_text(sprite_asset("Icon", f"{SOURCE_GUID}: 21300000", settings=67 | (1 << 2)))
            result = split_atlas_sprites(root)
            self.assertTrue(sprite.exists())
            self.assertTrue((root / "Assets" / "Texture2D" / f"{PAGE}.png").exists())
            self.assertEqual((result.sprites_split, result.skipped_packing, result.pages_kept), (0, 1, 1))


class CatalogSceneTests(unittest.TestCase):
    def test_guid_named_scene_and_its_folder_move_to_the_catalog_path(self) -> None:
        guid = "d" * 32
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            assets = project / "Assets"
            (assets / guid).mkdir(parents=True)
            (assets / f"{guid}.unity").write_text("scene")
            (assets / f"{guid}.unity.meta").write_text("guid: " + "e" * 32 + "\n")
            (assets / guid / "LightingData.asset").write_text("data")
            moves, guids = catalog_scene_moves(project, {guid: "Assets/_Resources/Scene/Other/Main.unity"})
            self.assertEqual(moves[f"Assets/{guid}.unity"], "Assets/_Resources/Scene/Other/Main.unity")
            self.assertEqual(moves[f"Assets/{guid}/LightingData.asset"], "Assets/_Resources/Scene/Other/Main/LightingData.asset")
            self.assertEqual(guids, {"e" * 32: guid})

    def test_guid_named_shader_moves_to_its_catalog_path(self) -> None:
        guid = "f" * 32
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "Assets").mkdir()
            (project / "Assets" / f"{guid}.shader").write_text("Shader \"UI/Saturation\" {}")
            (project / "Assets" / f"{guid}.shader.meta").write_text("guid: " + "1" * 32 + "\n")
            moves, guids = catalog_scene_moves(project, {guid: "Assets/_Art/Shaders/UI/UISaturation.shader"})
            self.assertEqual(moves, {f"Assets/{guid}.shader": "Assets/_Art/Shaders/UI/UISaturation.shader"})
            self.assertEqual(guids, {"1" * 32: guid})


if __name__ == "__main__":
    unittest.main()
