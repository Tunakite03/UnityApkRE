"""Sprite sheet manifest: every Sprite's rect, pivot, border and pixels per unit, grouped by texture.

The package output exports each sprite as a PNG but keeps its sheet metadata only in
``game_model.json``. This module reads those Sprite records together with the Texture2D they
reference (``m_RD.texture``) and the exported texture path from ``catalog.json``, so a sprite
sheet can be rebuilt without AssetRipper. Sprites whose texture was not exported are still
listed, with ``texture`` set to null.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError
from uare.game_model import Json, ObjectRecord, identity, load_model, outgoing
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.sprite_sheets")
MANIFEST_NAME = "sprite_sheets.json"


class SpriteSheetError(UareError):
    """The package output lacks the files needed for sprite sheet manifests."""


@dataclass(frozen=True)
class SpriteSheetResult:
    textures: int
    sprites: int
    sprites_without_texture: int
    out_dir: str


def _number(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _vector(value: object, keys: tuple[str, ...], defaults: tuple[float, ...]) -> dict[str, float]:
    data = value if isinstance(value, dict) else {}
    return {k: _number(data.get(k), d) for k, d in zip(keys, defaults)}


def _exported_textures(catalog_path: Path) -> dict[str, str]:
    """Texture2D object id -> exported PNG path (relative to the package output)."""
    if not catalog_path.is_file():
        raise SpriteSheetError(f"sprite sheets need catalog.json next to game_model.json; missing {catalog_path}")
    entries = json.loads(catalog_path.read_text(encoding="utf-8"))
    return {
        str(e["object_id"]): str(e["output"])
        for e in entries
        if e.get("class_name") == "Texture2D" and e.get("output") and e.get("object_id")
    }


def _key_text(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _atlas_renders(atlas: ObjectRecord) -> dict[str, tuple[str | None, object]]:
    """SpriteAtlas render data: render key -> (texture object id in the same file, textureRect)."""
    renders: dict[str, tuple[str | None, object]] = {}
    for entry in atlas.data.get("m_RenderDataMap") or []:
        if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], dict)):
            continue
        key, render = entry
        texture = render.get("texture")
        path_id = texture.get("m_PathID") if isinstance(texture, dict) else None
        texture_id = identity(atlas.source, atlas.serialized_file, int(path_id)) if path_id else None
        renders[_key_text(key)] = (texture_id, render.get("textureRect"))
    return renders


def _sprite_record(sprite: ObjectRecord, sprite_names: dict[str, str],
                   rect_override: object = None) -> dict[str, Json]:
    data = sprite.data
    rect = rect_override if isinstance(rect_override, dict) else data.get("m_Rect")
    return {
        "id": sprite.id,
        "name": sprite_names[sprite.id],
        "pathId": sprite.path_id,
        "rect": _vector(rect, ("x", "y", "width", "height"), (0.0, 0.0, 0.0, 0.0)),
        "pivot": _vector(data.get("m_Pivot"), ("x", "y"), (0.5, 0.5)),
        "border": _vector(data.get("m_Border"), ("x", "y", "z", "w"), (0.0, 0.0, 0.0, 0.0)),
        "pixelsPerUnit": _number(data.get("m_PixelsToUnits"), 100.0),
    }


def build_manifest(assets_dir: Path) -> dict[str, list[dict[str, Json]]]:
    """Group the package's Sprite records by the Texture2D they cut from."""
    model_path = assets_dir / "game_model.json"
    if not model_path.is_file():
        raise SpriteSheetError(f"sprite sheets need a package output with game_model.json; source={assets_dir}")
    model = load_model(model_path)
    exported = _exported_textures(assets_dir / "catalog.json")
    edges = outgoing(model)
    by_id = {o.id: o for o in model.objects}
    atlas_cache: dict[str, dict[str, tuple[str | None, object]]] = {}
    # (sprite, texture object id or None, rect override inside that texture)
    placed: list[tuple[ObjectRecord, str | None, object]] = []
    for obj in model.objects:
        if obj.class_name != "Sprite":
            continue
        texture = next((r.target_id for r in edges.get(obj.id, ()) if r.field == "m_RD.texture"
                        and r.status == "resolved" and r.target_id), None)
        if texture is not None:
            placed.append((obj, texture, None))
            continue
        # A sprite packed into a SpriteAtlas has a null texture; its render data is in the atlas.
        atlas_id = next((r.target_id for r in edges.get(obj.id, ()) if r.field == "m_SpriteAtlas"
                         and r.status == "resolved" and r.target_id), None)
        atlas = by_id.get(atlas_id) if atlas_id else None
        if atlas is None or atlas.class_name != "SpriteAtlas":
            placed.append((obj, None, None))
            continue
        renders = atlas_cache.setdefault(atlas.id, _atlas_renders(atlas))
        hit = renders.get(_key_text(obj.data.get("m_RenderDataKey")))
        if hit is None or hit[0] is None:
            placed.append((obj, None, None))
        else:
            placed.append((obj, hit[0], hit[1]))
    by_texture: dict[str, list[tuple[ObjectRecord, object]]] = defaultdict(list)
    for obj, texture, rect in placed:
        by_texture[texture or ""].append((obj, rect))
    manifest: dict[str, list[dict[str, Json]]] = {}
    for texture_id, entries in by_texture.items():
        entries.sort(key=lambda e: (e[0].source, e[0].serialized_file, e[0].path_id))
        names: dict[str, str] = {}
        used: set[str] = set()
        for sprite, _ in entries:
            name = sprite.name or f"sprite_{sprite.path_id}"
            if name in used:
                name = f"{name}_{sprite.path_id}"
            used.add(name)
            names[sprite.id] = name
        key = exported.get(texture_id, "") if texture_id else ""
        manifest.setdefault(key, []).extend(_sprite_record(s, names, rect) for s, rect in entries)
    return manifest


def write_manifest(assets_dir: Path, out_dir: Path) -> SpriteSheetResult:
    """Write `sprite_sheets.json` into `out_dir` (must be new or empty)."""
    manifest = build_manifest(assets_dir)
    textures = {k: v for k, v in manifest.items() if k}
    unlinked = manifest.get("", [])
    document = {
        "textures": [{"texture": path, "sprites": sprites} for path, sprites in sorted(textures.items())],
        "spritesWithoutTexture": unlinked,
    }
    with staged_output(out_dir) as staging:
        (staging / MANIFEST_NAME).write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    result = SpriteSheetResult(
        textures=len(textures),
        sprites=sum(len(v) for v in textures.values()),
        sprites_without_texture=len(unlinked),
        out_dir=str(out_dir),
    )
    event(LOG, logging.INFO, "sprite_sheets_written", textures=result.textures, sprites=result.sprites,
          sprites_without_texture=result.sprites_without_texture)
    return result


def summarize(result: SpriteSheetResult) -> list[str]:
    return [
        f"textures          : {result.textures} with exported sprite sheets",
        f"sprites           : {result.sprites}",
        f"without texture   : {result.sprites_without_texture}",
        f"output            : {result.out_dir}/{MANIFEST_NAME}",
    ]
