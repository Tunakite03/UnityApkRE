import json
import tempfile
import unittest
from pathlib import Path

from uare.game_model import FileRecord, GameModel, ObjectRecord, identity, save_model
from uare.sprite_sheets import build_manifest, write_manifest


def _sprite_fixture(assets: Path, sprite_texture_linked: bool) -> tuple[str, str]:
    texture = ObjectRecord(identity("Data/sheet", "sheet", 1), "Data/sheet", "sheet", 1,
                           "Texture2D", "Atlas", {"m_Width": 256, "m_Height": 256}, "complete", (), None)
    sprite_a = ObjectRecord(identity("Data/sheet", "sheet", 2), "Data/sheet", "sheet", 2, "Sprite", "Icon",
                            {"m_RD": {"texture": {"m_FileID": 0, "m_PathID": 1}},
                             "m_Rect": {"x": 0.0, "y": 0.0, "width": 64.0, "height": 32.0},
                             "m_Pivot": {"x": 0.5, "y": 0.25}, "m_Border": {"x": 1.0, "y": 2.0, "z": 3.0, "w": 4.0},
                             "m_PixelsToUnits": 50.0}, "complete", (), None)
    sprite_b = ObjectRecord(identity("Data/sheet", "sheet", 3), "Data/sheet", "sheet", 3, "Sprite", "Icon",
                            {"m_RD": {"texture": {"m_FileID": 0, "m_PathID": 1}},
                             "m_Rect": {"x": 64.0, "y": 0.0, "width": 32.0, "height": 32.0}}, "complete", (), None)
    if not sprite_texture_linked:
        for sprite in (sprite_a, sprite_b):
            sprite.data.pop("m_RD")
    model = GameModel((FileRecord("Data/sheet", "sheet", ()),), (texture, sprite_a, sprite_b), (), ())
    save_model(assets / "game_model.json", model)
    (assets / "catalog.json").write_text(json.dumps([
        {"object_id": texture.id, "class_name": "Texture2D", "output": "shared/textures/Atlas.png"}
    ]), encoding="utf-8")
    return texture.id, sprite_a.id


def _atlas_fixture(assets: Path) -> None:
    key = [{"data[0]": 1, "data[1]": 2, "data[2]": 3, "data[3]": 4}, 21300000]
    texture = ObjectRecord(identity("Data/atlas", "atlas", 10), "Data/atlas", "atlas", 10,
                           "Texture2D", "AtlasTex", {"m_Width": 512, "m_Height": 512}, "complete", (), None)
    atlas = ObjectRecord(identity("Data/atlas", "atlas", 20), "Data/atlas", "atlas", 20, "SpriteAtlas", "Atlas",
                         {"m_RenderDataMap": [[key, {"texture": {"m_FileID": 0, "m_PathID": 10},
                                                     "textureRect": {"x": 100.0, "y": 50.0,
                                                                     "width": 40.0, "height": 30.0}}]]},
                         "complete", (), None)
    packed = ObjectRecord(identity("Data/atlas", "atlas", 30), "Data/atlas", "atlas", 30, "Sprite", "Packed",
                          {"m_RD": {"texture": {"m_FileID": 0, "m_PathID": 0}},
                           "m_SpriteAtlas": {"m_FileID": 0, "m_PathID": 20},
                           "m_RenderDataKey": key,
                           "m_Rect": {"x": 0.0, "y": 0.0, "width": 9.0, "height": 9.0},
                           "m_PixelsToUnits": 100.0}, "complete", (), None)
    model = GameModel((FileRecord("Data/atlas", "atlas", ()),), (texture, atlas, packed), (), ())
    save_model(assets / "game_model.json", model)
    (assets / "catalog.json").write_text(json.dumps([
        {"object_id": texture.id, "class_name": "Texture2D", "output": "shared/textures/AtlasTex.png"}
    ]), encoding="utf-8")


class SpriteSheetManifestTests(unittest.TestCase):
    def test_atlas_packed_sprite_uses_atlas_texture_and_rect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assets = Path(tmpdir) / "assets"
            assets.mkdir()
            _atlas_fixture(assets)
            manifest = build_manifest(assets)
            self.assertEqual(set(manifest), {"shared/textures/AtlasTex.png"})
            sprite = manifest["shared/textures/AtlasTex.png"][0]
            self.assertEqual(sprite["rect"], {"x": 100.0, "y": 50.0, "width": 40.0, "height": 30.0})

    def test_sprites_are_grouped_under_their_exported_texture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assets = Path(tmpdir) / "assets"
            assets.mkdir()
            _sprite_fixture(assets, sprite_texture_linked=True)
            manifest = build_manifest(assets)
            self.assertEqual(set(manifest), {"shared/textures/Atlas.png"})
            sprites = manifest["shared/textures/Atlas.png"]
            self.assertEqual([s["name"] for s in sprites], ["Icon", "Icon_3"])
            first = sprites[0]
            self.assertEqual(first["rect"], {"x": 0.0, "y": 0.0, "width": 64.0, "height": 32.0})
            self.assertEqual(first["pivot"], {"x": 0.5, "y": 0.25})
            self.assertEqual(first["border"], {"x": 1.0, "y": 2.0, "z": 3.0, "w": 4.0})
            self.assertEqual(first["pixelsPerUnit"], 50.0)

    def test_sprites_without_a_texture_are_kept_apart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assets = Path(tmpdir) / "assets"
            assets.mkdir()
            _sprite_fixture(assets, sprite_texture_linked=False)
            manifest = build_manifest(assets)
            self.assertEqual(set(manifest), {""})
            self.assertEqual(len(manifest[""]), 2)

    def test_write_manifest_writes_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assets = Path(tmpdir) / "assets"
            assets.mkdir()
            _sprite_fixture(assets, sprite_texture_linked=True)
            out = Path(tmpdir) / "out"
            result = write_manifest(assets, out)
            self.assertEqual((result.textures, result.sprites, result.sprites_without_texture), (1, 2, 0))
            document = json.loads((out / "sprite_sheets.json").read_text(encoding="utf-8"))
            self.assertEqual(document["textures"][0]["texture"], "shared/textures/Atlas.png")
            self.assertEqual(document["spritesWithoutTexture"], [])


if __name__ == "__main__":
    unittest.main()
