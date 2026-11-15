"""Original project paths for assets the build stores without one.

A build keeps an asset's project path only where something addresses the asset by it: an AssetBundle container
key (`blueprint.original_asset_paths`). An asset that is only a dependency, of a built-in scene
(`sharedassetsN.assets`) or of a bundle, is written without any path. Two kinds of evidence still tie such an
object to a recorded path:

* Sprite source GUID. A Sprite keeps the GUID of the texture asset it was cut from in `m_RenderDataKey`. When
  the Addressables catalog names that GUID, the path is the sprite's source texture. A sprite that is not packed
  into an atlas renders from that same texture, so its texture object gets the path too.
* Identical copy. Unity writes a separate copy of an asset into every build unit that depends on it, so a
  pathless object can be a copy of an object whose path is recorded. A copy is accepted only when class, name
  and the whole serialized content agree, exported payload files have the same bytes, the assets it references
  agree in the same way (path IDs differ per file, so references are followed, three levels deep, instead of
  compared), and every such copy with a recorded path names the same path. A payload that was not exported
  cannot be compared: the object holding it is never matched, and neither is an object referencing it unless
  that object's own exported file is compared.

The first is identity the build records. The second is inference: two different source assets with the same
name and identical content cannot be told apart, so each result carries its evidence.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from uare.game_model import GameModel, Json, ObjectRecord, Reference

SPRITE_KEY_EVIDENCE = "Sprite.m_RenderDataKey GUID + Addressables catalog path"
COPY_EVIDENCE = "content-identical copy of an asset with a recorded path"

# Hierarchy objects and build bookkeeping; components are skipped by their m_GameObject field.
_NOT_ASSETS = frozenset({"GameObject", "Transform", "RectTransform", "MonoScript", "AssetBundle", "PreloadData"})
# Fields that hold, or point at, payload bytes. One copy may store them inline and another in a resource file,
# so they are left out of the content comparison and the exported file is compared instead.
_PAYLOAD_FIELDS = frozenset({"image data", "m_StreamData", "m_Resource", "m_ExternalResources"})
# An export with this suffix is a dump of the serialized fields (path IDs included), not a payload file.
_DUMP_SUFFIX = ".json"
# How many levels of referenced assets take part in the comparison. An asset that only holds references (a Spine
# SkeletonDataAsset, a material) differs from its namesakes in what those references contain.
_REFERENCE_DEPTH = 3


@dataclass(frozen=True)
class Origin:
    """An original project path derived for an object the build stores without one."""
    path: str
    evidence: str
    copy_of: str | None  # the object whose recorded path this is, when the path comes from an identical copy


@dataclass(frozen=True)
class DerivedOrigins:
    origins: dict[str, Origin]
    ambiguous: int  # objects whose identical copies record several different paths; left without a path


def sprite_source_guid(sprite: ObjectRecord) -> str | None:
    """GUID of the texture asset a Sprite was cut from, in Unity's hex spelling; None without a usable key."""
    key = sprite.data.get("m_RenderDataKey")
    if not isinstance(key, list) or len(key) != 2 or not isinstance(key[0], dict):
        return None
    digits: list[str] = []
    for index in range(4):
        word = key[0].get(f"data[{index}]")
        if type(word) is not int or not 0 <= word < 1 << 32:
            return None
        # Unity spells each byte low nibble first.
        digits.extend(f"{byte & 15:x}{byte >> 4:x}" for byte in word.to_bytes(4, "little"))
    guid = "".join(digits)
    return guid if guid.strip("0") else None


def _atlas_packed(sprite: ObjectRecord) -> bool:
    atlas = sprite.data.get("m_SpriteAtlas")
    return bool(sprite.data.get("m_AtlasTags")) or (isinstance(atlas, dict) and atlas.get("m_PathID") != 0)


def _sprite_key_origins(model: GameModel, edges: Mapping[str, tuple[Reference, ...]], recorded: Mapping[str, str],
                        catalog_paths: Mapping[str, str]) -> dict[str, Origin]:
    origins: dict[str, Origin] = {}
    textures: dict[str, set[str]] = defaultdict(set)
    for obj in model.objects:
        if obj.class_name != "Sprite":
            continue
        guid = sprite_source_guid(obj)
        path = catalog_paths.get(guid) if guid else None
        if path is None:
            continue
        if obj.id not in recorded:
            origins[obj.id] = Origin(path, SPRITE_KEY_EVIDENCE, None)
        if not _atlas_packed(obj):
            texture = next((r.target_id for r in edges.get(obj.id, ()) if r.field == "m_RD.texture"), None)
            if texture is not None and texture not in recorded:
                textures[texture].add(path)
    for texture, paths in textures.items():
        if len(paths) == 1:
            origins[texture] = Origin(next(iter(paths)), SPRITE_KEY_EVIDENCE, None)
    return origins


class _Contents:
    """Content digests of objects, memoised per comparison depth.

    At depth 0 a reference counts as the class and name of its target; at depth n as the target's digest at depth
    n - 1. Depths are computed bottom-up, so reference cycles need no special case.
    """

    def __init__(self, model: GameModel, by_id: Mapping[str, ObjectRecord],
                 edges: Mapping[str, tuple[Reference, ...]], assets_dir: Path) -> None:
        self._by_id = by_id
        self._edges = edges
        self._assets_dir = assets_dir
        self._externals = {(file.source, file.serialized_file): file.externals for file in model.files}
        self._payloads: dict[str, bytes | None] = {}
        self._digests: dict[tuple[str, int], str | None] = {}

    def digest(self, obj: ObjectRecord, depth: int) -> str | None:
        """Digest of everything the build holds for `obj`; None when a payload it depends on cannot be compared."""
        key = (obj.id, depth)
        if key not in self._digests:
            self._digests[key] = self._compute(obj, depth)
        return self._digests[key]

    def _payload(self, obj: ObjectRecord) -> bytes | None:
        """Digest of the exported payload file (empty without one); None when the payload was not exported."""
        if obj.id not in self._payloads:
            payload: bytes | None = b""
            if obj.output is not None and not obj.output.endswith(_DUMP_SUFFIX):
                exported = self._assets_dir / obj.output
                payload = hashlib.sha256(exported.read_bytes()).digest() if exported.is_file() else None
            elif _PAYLOAD_FIELDS & obj.data.keys():
                payload = None
            self._payloads[obj.id] = payload
        return self._payloads[obj.id]

    def _compute(self, obj: ObjectRecord, depth: int) -> str | None:
        payload = self._payload(obj)
        if payload is None:
            return None
        externals = self._externals.get((obj.source, obj.serialized_file), ())
        tokens: dict[str, Json] = {}
        for ref in self._edges.get(obj.id, ()):
            target = self._by_id[ref.target_id] if ref.target_id is not None else None
            if ref.status == "null":
                tokens[ref.field] = None
            elif target is None:
                external = externals[ref.file_id - 1] if 0 < ref.file_id <= len(externals) else ""
                file_name = posixpath.basename(external.replace("\\", "/")).casefold()
                tokens[ref.field] = [ref.status, file_name, ref.path_id]
            elif depth == 0 or not _is_asset(target):
                tokens[ref.field] = [target.class_name, target.name]
            else:
                content = self.digest(target, depth - 1)
                if content is None and not payload:
                    return None
                # An object whose own exported file is compared keeps a target it cannot compare by name: a font
                # file is decisive without its generated atlas texture.
                tokens[ref.field] = content if content is not None else [target.class_name, target.name]
        body = json.dumps(_normalized(obj.data, "", tokens), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload + body.encode("ascii")).hexdigest()


def _is_asset(obj: ObjectRecord) -> bool:
    return obj.class_name not in _NOT_ASSETS and "m_GameObject" not in obj.data


def _normalized(value: Json, field: str, tokens: Mapping[str, Json]) -> Json:
    """`value` with each reference replaced by its token; field paths follow `game_model.pointers`."""
    if isinstance(value, dict):
        if "m_FileID" in value and "m_PathID" in value:
            return {"$ref": tokens.get(field)}
        return {key: _normalized(item, f"{field}.{key}" if field else key, tokens)
                for key, item in value.items() if field or key not in _PAYLOAD_FIELDS}
    if isinstance(value, list):
        return [_normalized(item, f"{field}[{index}]", tokens) if isinstance(item, (dict, list)) else item
                for index, item in enumerate(value)]
    return value


def _copy_origins(model: GameModel, by_id: Mapping[str, ObjectRecord], edges: Mapping[str, tuple[Reference, ...]],
                  recorded: Mapping[str, str], assets_dir: Path) -> tuple[dict[str, Origin], int]:
    groups: dict[tuple[str, str], list[ObjectRecord]] = defaultdict(list)
    for obj in model.objects:
        if obj.name and _is_asset(obj):
            groups[obj.class_name, obj.name].append(obj)
    contents = _Contents(model, by_id, edges, assets_dir)
    origins: dict[str, Origin] = {}
    ambiguous = 0
    # Only a name shared by a recorded and a pathless object is worth hashing.
    for members in groups.values():
        pathless = [obj for obj in members if obj.id not in recorded]
        if not pathless or len(pathless) == len(members):
            continue
        by_content: dict[str, list[str]] = defaultdict(list)
        for obj in members:
            if obj.id in recorded:
                content = contents.digest(obj, _REFERENCE_DEPTH)
                if content is not None:
                    by_content[content].append(obj.id)
        for obj in pathless:
            content = contents.digest(obj, _REFERENCE_DEPTH) if by_content else None
            copies = by_content.get(content, []) if content is not None else []
            paths = {recorded[oid] for oid in copies}
            if len(paths) == 1:
                origins[obj.id] = Origin(next(iter(paths)), COPY_EVIDENCE, min(copies))
            elif paths:
                ambiguous += 1
    return origins, ambiguous


def derive_origins(model: GameModel, by_id: Mapping[str, ObjectRecord], edges: Mapping[str, tuple[Reference, ...]],
                   recorded: Mapping[str, str], catalog_paths: Mapping[str, str], assets_dir: Path) -> DerivedOrigins:
    """Paths for objects that `recorded` (object id -> recorded original path) does not cover.

    `catalog_paths` maps asset GUIDs to paths (an Addressables catalog; empty without one). Exported payload
    files are read under `assets_dir`. A sprite's own source GUID takes precedence over a copy.
    """
    copies, ambiguous = _copy_origins(model, by_id, edges, recorded, assets_dir)
    return DerivedOrigins({**copies, **_sprite_key_origins(model, edges, recorded, catalog_paths)}, ambiguous)
