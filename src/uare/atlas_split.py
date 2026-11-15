"""Split SpriteAtlas pages (`sactx-*` textures) of an AssetRipper project into one PNG per sprite.

A build keeps packed sprites only inside atlas pages; AssetRipper exports each page as one texture and each
sprite as a `Sprite` asset pointing into it. For reconstruction the sprites are wanted as separate images, so a
new SpriteAtlas can be built later. Every rectangle-packed, unrotated sprite of an atlas page becomes a PNG
cropped at its `m_RD.textureRect`, imported as a single sprite with the original pivot, border and pixels per
unit (pivot and border are moved into the cropped rectangle when the packer trimmed the sprite).

Identity: the PNG takes the source texture GUID recorded in `m_RenderDataKey` when that key names the main
sprite of a single-sprite texture (file ID 21300000) used by no other sprite; it then also goes to that
texture's original path when the Addressables catalog records one. Otherwise it keeps the Sprite asset's GUID.
Either way references keep file ID 21300000 and are rewritten from the Sprite asset (`type: 2`) to the image
(`type: 3`). The Sprite asset is removed, and an atlas page is removed once nothing references it.
Tight-packed or rotated sprites are left as they are and reported.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from uare.errors import UareError
from uare.logs import event

LOG = logging.getLogger("uare.atlas_split")

SPRITE_FILE_ID = 21300000
_GUID_LINE = re.compile(r"^guid: ([0-9a-f]{32})$", re.M)
_REF = re.compile(rb"guid: ([0-9a-f]{32}), type: (\d)")
_ATLAS_NAME = re.compile(r"^sactx-\d+-\d+x\d+-(?:[^-]+)-(.+)-[0-9a-f]{8}$")
_SAFE = re.compile(r"[^A-Za-z0-9._ +()\[\]-]+")
UNMAPPED_FOLDER = "Assets/_Unmapped/AtlasSprites"


class AtlasSplitError(UareError):
    """An AssetRipper project cannot be split into per-sprite images."""


@dataclass(frozen=True)
class AtlasSplitResult:
    atlas_pages: int
    sprites_split: int
    original_guids: int
    original_paths: int
    skipped_packing: int
    pages_removed: int
    pages_kept: int


@dataclass(frozen=True)
class _Sprite:
    asset: Path
    guid: str
    name: str
    page: str
    rect: tuple[float, float, float, float]  # m_Rect
    texture_rect: tuple[float, float, float, float]
    texture_rect_offset: tuple[float, float]
    pivot: tuple[float, float]
    border: tuple[float, float, float, float]
    pixels_per_unit: float
    settings_raw: int
    source_guid: str | None
    source_file_id: int | None


def _number(body: str, pattern: str, what: str, path: Path) -> float:
    match = re.search(pattern, body, re.M)
    if match is None:
        raise AtlasSplitError(f"Sprite asset lacks {what}; path={path}")
    return float(match[1].replace("Infinity", "inf"))


def _vector(body: str, key: str, fields: str, path: Path) -> tuple[float, ...]:
    match = re.search(rf"^  {key}: \{{(.*)\}}$", body, re.M)
    if match is None:
        raise AtlasSplitError(f"Sprite asset lacks {key}; path={path}")
    values = dict(re.findall(r"(\w+): ([^,}]+)", match[1]))
    return tuple(float(values[f]) for f in fields)


def _rect(block: str, path: Path) -> tuple[float, float, float, float]:
    return tuple(_number(block, rf"^\s+{f}: (\S+)$", f"rect {f}", path) for f in ("x", "y", "width", "height"))  # type: ignore[return-value]


def _meta_guid(meta: Path) -> str:
    match = _GUID_LINE.search(meta.read_text(encoding="utf-8", errors="replace"))
    if match is None:
        raise AtlasSplitError(f"Meta file has no GUID; path={meta}")
    return match[1]


def read_sprite(asset: Path, pages: Mapping[str, str]) -> _Sprite | None:
    """The sprite of an AssetRipper Sprite asset when it points into one of `pages` (GUID -> atlas page name)."""
    text = asset.read_text(encoding="utf-8", errors="replace")
    rd = text.split("\n  m_RD:\n", 1)
    if len(rd) != 2:
        return None
    texture = re.search(r"^    texture: \{fileID: \d+, guid: ([0-9a-f]{32})", rd[1], re.M)
    if texture is None or texture[1] not in pages:
        return None
    head, body = rd[0], rd[1].split("\n  m_AtlasRD:\n", 1)[0]
    if "\n  m_Rect:\n" not in head or "\n    textureRect:\n" not in body:
        raise AtlasSplitError(f"Sprite asset lacks m_Rect or textureRect; path={asset}")
    rect_block = head.split("\n  m_Rect:\n", 1)[1].split("\n  m_Offset:", 1)[0]
    tex_block = body.split("\n    textureRect:\n", 1)[1].split("\n    textureRectOffset:", 1)[0]
    offset = re.search(r"^    textureRectOffset: \{x: (\S+), y: (\S+)\}$", body, re.M)
    settings = re.search(r"^    settingsRaw: (\d+)$", body, re.M)
    if offset is None or settings is None:
        raise AtlasSplitError(f"Sprite asset lacks textureRectOffset or settingsRaw; path={asset}")
    key = re.search(r"^  m_RenderDataKey:\n    ([0-9a-f]{32}): (-?\d+)$", head, re.M)
    name = re.search(r"^  m_Name: ?(.*)$", head, re.M)
    meta = asset.with_name(asset.name + ".meta")
    return _Sprite(
        asset, _meta_guid(meta), (name[1].strip() if name else asset.stem), texture[1],
        _rect(rect_block, asset), _rect(tex_block, asset), (float(offset[1]), float(offset[2])),
        _vector(head, "m_Pivot", "xy", asset), _vector(head, "m_Border", "xyzw", asset),  # type: ignore[arg-type]
        _number(head, r"^  m_PixelsToUnits: (\S+)$", "m_PixelsToUnits", asset), int(settings[1]),
        key[1] if key else None, int(key[2]) if key else None)


def _packed_rectangle(settings_raw: int) -> bool:
    """Packed (bit 0), rectangle packing (bit 1 set) and no packing rotation (bits 2-5 clear)."""
    return settings_raw & 1 == 1 and settings_raw & 2 == 2 and (settings_raw >> 2) & 0xF == 0


def crop_box(sprite: _Sprite, page_height: int) -> tuple[int, int, int, int]:
    """PIL box of the sprite in its page: Unity rects start at the bottom-left, images at the top-left."""
    x, y, w, h = sprite.texture_rect
    left, right = round(x), round(x + w)
    top, bottom = page_height - round(y + h), page_height - round(y)
    if right <= left or bottom <= top:
        raise AtlasSplitError(f"Sprite has an empty texture rect; path={sprite.asset}")
    return left, top, right, bottom


def import_settings(sprite: _Sprite, width: int, height: int) -> tuple[tuple[float, float], tuple[float, float, float, float]]:
    """Pivot and border inside the cropped image: the packer may trim transparent edges off the original rect."""
    _, _, rect_w, rect_h = sprite.rect
    off_x, off_y = sprite.texture_rect_offset
    pivot = ((sprite.pivot[0] * rect_w - off_x) / width, (sprite.pivot[1] * rect_h - off_y) / height)
    left, bottom, right, top = sprite.border
    border = (max(0.0, left - off_x), max(0.0, bottom - off_y),
              max(0.0, right - (rect_w - off_x - width)), max(0.0, top - (rect_h - off_y - height)))
    return pivot, border


def sprite_meta(page_meta: str, guid: str, pivot: tuple[float, float], border: tuple[float, float, float, float],
                pixels_per_unit: float) -> str:
    """The atlas page's importer settings, as a single sprite with this sprite's pivot, border and PPU."""
    replacements = (
        (r"^guid: [0-9a-f]{32}$", f"guid: {guid}"),
        (r"^  spriteMode: \d+$", "  spriteMode: 1"),
        (r"^  alignment: \d+$", "  alignment: 9"),
        (r"^  spritePivot: \{.*\}$", f"  spritePivot: {{x: {pivot[0]:.6g}, y: {pivot[1]:.6g}}}"),
        (r"^  spritePixelsToUnits: \S+$", f"  spritePixelsToUnits: {pixels_per_unit:.7g}"),
        (r"^  spriteBorder: \{.*\}$", "  spriteBorder: {x: %.6g, y: %.6g, z: %.6g, w: %.6g}" % border),
        (r"^  textureType: \d+$", "  textureType: 8"),
    )
    text = page_meta
    for pattern, value in replacements:
        text, count = re.subn(pattern, value, text, count=1, flags=re.M)
        if count != 1:
            raise AtlasSplitError(f"Atlas page meta lacks a TextureImporter field; pattern={pattern}")
    return text


def _file_name(name: str, used: set[str]) -> str:
    stem = _SAFE.sub("_", name).strip(" .") or "sprite"
    candidate, counter = stem, 1
    while candidate.casefold() in used:
        candidate = f"{stem}_{counter}"
        counter += 1
    used.add(candidate.casefold())
    return candidate + ".png"


def split_atlas_sprites(project: Path, original_paths: Mapping[str, str] | None = None) -> AtlasSplitResult:
    """Split every `sactx-*` page under `project/Assets` in place (the project must be a working copy).

    `original_paths` maps asset GUIDs to original project paths (an Addressables catalog).
    """
    from PIL import Image

    assets = project / "Assets"
    pages: dict[str, Path] = {}
    names: dict[str, str] = {}
    for image in sorted(assets.rglob("sactx-*.png")):
        meta = image.with_name(image.name + ".meta")
        match = _ATLAS_NAME.fullmatch(image.stem)
        if not meta.is_file() or match is None:
            continue
        guid = _meta_guid(meta)
        pages[guid], names[guid] = image, match[1]
    sprites = [s for s in (read_sprite(p, names) for p in sorted(assets.rglob("*.asset"))) if s is not None]
    used_guids = {_meta_guid(m) for m in assets.rglob("*.meta")}
    source_uses = Counter(s.source_guid for s in sprites if s.source_guid)
    renames: dict[str, str] = {}
    split = original_guids = original_placed = skipped = 0
    folder_names: dict[str, set[str]] = defaultdict(set)
    taken_paths = {p.relative_to(project).as_posix().casefold() for p in assets.rglob("*") if p.is_file()}
    images: dict[str, Image.Image] = {}
    try:
        for sprite in sprites:
            if not _packed_rectangle(sprite.settings_raw):
                skipped += 1
                continue
            page = images.get(sprite.page)
            if page is None:
                page = images[sprite.page] = Image.open(pages[sprite.page]).convert("RGBA")
            box = crop_box(sprite, page.height)
            if box[2] > page.width or box[3] > page.height or box[0] < 0 or box[1] < 0:
                raise AtlasSplitError(f"Sprite rect lies outside its atlas page; path={sprite.asset}")
            crop = page.crop(box)
            guid, target = sprite.guid, None
            single = (sprite.source_guid and sprite.source_file_id == SPRITE_FILE_ID and source_uses[sprite.source_guid] == 1
                      and sprite.source_guid not in used_guids)
            if single:
                guid = str(sprite.source_guid)
                original_guids += 1
                original = (original_paths or {}).get(guid)
                if original and original.startswith("Assets/") and ".." not in original:
                    candidate = str(PurePosixPath(original).with_suffix(".png"))
                    if candidate.casefold() not in taken_paths:
                        target = candidate
            if target is None:
                folder = f"{UNMAPPED_FOLDER}/{_SAFE.sub('_', names[sprite.page]).strip(' .') or 'atlas'}"
                target = f"{folder}/{_file_name(sprite.name, folder_names[folder])}"
            else:
                original_placed += 1
            taken_paths.add(target.casefold())
            out = project / target
            out.parent.mkdir(parents=True, exist_ok=True)
            crop.save(out)
            pivot, border = import_settings(sprite, crop.width, crop.height)
            page_meta = pages[sprite.page].with_name(pages[sprite.page].name + ".meta").read_text(encoding="utf-8")
            out.with_name(out.name + ".meta").write_text(sprite_meta(page_meta, guid, pivot, border, sprite.pixels_per_unit),
                                                         encoding="utf-8")
            sprite.asset.unlink()
            sprite.asset.with_name(sprite.asset.name + ".meta").unlink()
            renames[sprite.guid] = guid
            used_guids.add(guid)
            split += 1
    finally:
        for image in images.values():
            image.close()
    referenced = _rewrite_references(assets, renames)
    removed = kept = 0
    for guid, image in pages.items():
        if guid in referenced:
            kept += 1
            continue
        image.unlink()
        image.with_name(image.name + ".meta").unlink()
        removed += 1
    result = AtlasSplitResult(len(pages), split, original_guids, original_placed, skipped, removed, kept)
    event(LOG, logging.INFO, "atlas_sprites_split", pages=result.atlas_pages, sprites=split, skipped=skipped,
          original_guids=original_guids, pages_removed=removed, pages_kept=kept)
    return result


def _rewrite_references(assets: Path, renames: Mapping[str, str]) -> set[str]:
    """Point sprite references at the new images (`type: 2` -> `type: 3`); returns every GUID still referenced."""
    encoded = {old.encode(): new.encode() for old, new in renames.items()}
    referenced: set[str] = set()

    def swap(match: re.Match[bytes]) -> bytes:
        new = encoded.get(match[1])
        if new is None:
            return match[0]
        return b"guid: " + new + b", type: 3"

    for path in assets.rglob("*"):
        if not path.is_file() or path.suffix == ".meta":
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192] or b"guid: " not in data:
            continue
        updated = _REF.sub(swap, data)
        if updated != data:
            path.write_bytes(updated)
        referenced.update(m[1].decode() for m in _REF.finditer(updated))
    return referenced


def summarize(result: AtlasSplitResult) -> list[str]:
    return [f"atlas pages       : {result.atlas_pages} ({result.pages_removed} removed, {result.pages_kept} still referenced)",
            f"sprites split     : {result.sprites_split} ({result.original_guids} with their source texture GUID, "
            f"{result.original_paths} at an original path, {result.skipped_packing} tight-packed or rotated left as is)"]
