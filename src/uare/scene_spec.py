"""Per-scene build specs for rebuilding scenes inside an existing Unity project.

A spec holds one scene's GameObjects in hierarchy order, its scene settings (RenderSettings,
LightmapSettings) and the assets the scene needs. Serialized fields are flattened to Unity property
paths (``a.b``, ``a.Array.size``, ``a.Array.data[0]``), and every object reference is resolved
through the game model: to a GameObject or component of the same scene, to an asset of the spec's
asset table, or to an explicit missing/unresolved entry.

The asset table has three kinds of entries, each with the path it gets inside the project:

* ``file``: an exported file copied as is (textures, sprites, meshes, fonts, audio, text);
* ``material``: a material created from its type-tree properties;
* ``scriptable``: a ScriptableObject (a MonoBehaviour asset such as a TMP font asset or a Spine
  SkeletonDataAsset) created from its type-tree fields when its script exists in the project.

The Unity-side builder (``unity/com.uare.scenebuilder``) reads a spec inside the Editor and builds the
scene with Unity's own API, so importers, materials and serialization belong to Unity, not to this module.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError
from uare.game_model import GameModel, Json, ObjectRecord, Reference, load_model, non_finite, outgoing
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.scene_spec")

SCHEMA = "uare.scene-spec"
SCHEMA_VERSION = 2

# Set structurally by the builder (hierarchy, ownership, script binding) or managed by Unity itself.
_STRUCTURAL = frozenset({
    "m_GameObject", "m_Father", "m_Children", "m_Component", "m_Script", "m_Name",
    "m_PrefabInstance", "m_PrefabAsset", "m_CorrespondingSourceObject", "m_PrefabParentObject",
    "m_PrefabInternal", "m_ObjectHideFlags", "m_EditorHideFlags", "m_EditorClassIdentifier",
})
# A ScriptableObject has no enabled flag; the type tree stores one because it is serialized as a MonoBehaviour.
_SCRIPTABLE_STRUCTURAL = _STRUCTURAL | {"m_Enabled"}
_TRANSFORMS = frozenset({"Transform", "RectTransform"})
_SCENE_SETTINGS = ("RenderSettings", "LightmapSettings")
# Exported files copied into the project, by class and accepted extension.
_FILES = {
    "Texture2D": (".png",), "Sprite": (".png",), "Mesh": (".obj",), "Font": (".ttf", ".otf"),
    "AudioClip": (".wav", ".ogg", ".mp3"),
}
# Extensions Unity imports as TextAsset. Another text file keeps its own name plus a suffix, so the TextAsset
# keeps the original name (Unity drops only the last extension).
_TEXT_EXTENSIONS = (".txt", ".bytes", ".json", ".csv", ".xml", ".yaml", ".html", ".htm", ".fnt")
_ALPHA8 = 1  # TextureFormat.Alpha8


class SceneSpecError(UareError):
    """A scene spec could not be built from the package output."""


@dataclass(frozen=True)
class SceneSpecResult:
    scenes: tuple[str, ...]
    game_objects: int
    components: int
    assets: int
    unresolved_references: int
    out_dir: str


def _row(path: str, kind: str, number: float = 0.0, text: str = "", ref_kind: str = "",
         ref_target: str = "", ref_class: str = "") -> dict[str, Json]:
    return {"path": path, "kind": kind, "number": number, "text": text,
            "refKind": ref_kind, "refTarget": ref_target, "refClass": ref_class}


def _text_import_path(output: str) -> str:
    """Project path of an exported text file, so Unity imports it as a TextAsset with the original name.

    ``.skel`` gets ``.txt`` instead of ``.bytes``: spine-unity's importer acts on ``*.skel.bytes`` and
    ``*.atlas.txt`` and can open modal dialogs, which would block an Editor driven from outside. The TextAsset
    name still contains ``.skel``, which is how spine-unity recognizes binary skeleton data.
    """
    if output.endswith(_TEXT_EXTENSIONS) and not output.endswith((".skel.bytes", ".atlas.txt")):
        return output
    return output + (".txt" if output.endswith(".skel") else ".bytes")


def _number(value: Json, default: float) -> float:
    spelling = non_finite(value)
    if spelling is not None:
        # Asset settings are JSON numbers in the builder; substituting the default would misstate the asset.
        raise SceneSpecError(f"Asset setting holds a non-finite float the builder cannot carry; value={spelling}")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _asset_entry(path: str, import_path: str, kind: str, target: ObjectRecord) -> dict[str, Json]:
    return {
        "path": path, "importPath": import_path, "kind": kind, "className": target.class_name, "name": target.name,
        "pixelsPerUnit": 100.0, "pivotX": 0.5, "pivotY": 0.5,
        "borderLeft": 0.0, "borderBottom": 0.0, "borderRight": 0.0, "borderTop": 0.0,
        "filterMode": -1, "wrapU": -1, "wrapV": -1, "mipmaps": False, "readable": False, "alphaOnly": False,
        "shaderName": "", "renderQueue": -1, "keywords": [], "textures": [], "floats": [], "ints": [], "colors": [],
        "scriptClass": "", "scriptNamespace": "", "scriptAssembly": "", "props": [],
    }


class _SpecBuilder:
    """Resolves one model's references; holds the asset table of the scene being written."""

    def __init__(self, model: GameModel, source_root: Path) -> None:
        self._by_id = {o.id: o for o in model.objects}
        self._edges = outgoing(model)
        self._source_root = source_root
        self._assets: dict[str, dict[str, Json]] = {}
        self._linkable: set[str] = set()
        self.unresolved = 0

    # -- references ---------------------------------------------------------------------------

    def _refs(self, oid: str) -> dict[str, Reference]:
        return {r.field: r for r in self._edges.get(oid, ())}

    def _target(self, oid: str, field: str) -> str | None:
        ref = self._refs(oid).get(field)
        return ref.target_id if ref is not None else None

    def _reference_row(self, path: str, ref: Reference | None) -> dict[str, Json]:
        if ref is None:
            self.unresolved += 1
            return _row(path, "ref", ref_kind="unresolved", ref_class="no reference recorded")
        if ref.status == "null":
            return _row(path, "ref", ref_kind="null")
        if ref.status != "resolved" or ref.target_id is None:
            self.unresolved += 1
            return _row(path, "ref", ref_kind="unresolved", ref_class=ref.status)
        target = self._by_id[ref.target_id]
        if ref.target_id in self._linkable:
            return _row(path, "ref", ref_kind="object", ref_target=ref.target_id, ref_class=target.class_name)
        asset = self._asset(target)
        if asset is None:
            return _row(path, "ref", ref_kind="missing", ref_target=target.name, ref_class=target.class_name)
        return _row(path, "ref", ref_kind="asset", ref_target=asset, ref_class=target.class_name)

    def _asset(self, target: ObjectRecord) -> str | None:
        """Register `target` in the asset table and return its source path, or None when it cannot be rebuilt."""
        output = target.output
        if not output or not output.startswith("shared/"):
            return None
        if output in self._assets:
            return output
        if target.class_name in _FILES and output.endswith(_FILES[target.class_name]):
            entry = _asset_entry(output, output, "file", target)
            self._assets[output] = entry
            if target.class_name == "Sprite":
                self._sprite_settings(target, entry)
            elif target.class_name == "Texture2D":
                self._texture_settings(target, entry)
        elif target.class_name == "TextAsset":
            self._assets[output] = _asset_entry(output, _text_import_path(output), "file", target)
        elif target.class_name == "Material" and output.endswith(".json"):
            entry = _asset_entry(output, output[:-len(".json")] + ".mat", "material", target)
            self._assets[output] = entry
            self._material_settings(target, entry)
        elif target.class_name == "MonoBehaviour" and output.endswith(".json") and self._is_asset(target):
            entry = _asset_entry(output, output[:-len(".json")] + ".asset", "scriptable", target)
            self._assets[output] = entry  # registered first, so reference cycles between assets terminate
            self._script_identity(target, entry)
            rows: list[dict[str, Json]] = []
            self._flatten(target.data, "", "", self._refs(target.id), rows, _SCRIPTABLE_STRUCTURAL)
            entry["props"] = rows
        else:
            return None
        return output

    def _is_asset(self, behaviour: ObjectRecord) -> bool:
        """A MonoBehaviour that belongs to no GameObject is a ScriptableObject asset."""
        ref = self._refs(behaviour.id).get("m_GameObject")
        return ref is None or ref.status == "null"

    def _script_identity(self, behaviour: ObjectRecord, entry: dict[str, Json]) -> None:
        script_id = self._target(behaviour.id, "m_Script")
        script = self._by_id.get(script_id) if script_id else None
        if script is not None:
            entry["scriptClass"] = str(script.data.get("m_ClassName", ""))
            entry["scriptNamespace"] = str(script.data.get("m_Namespace", ""))
            entry["scriptAssembly"] = str(script.data.get("m_AssemblyName", ""))

    def _sprite_settings(self, sprite: ObjectRecord, entry: dict[str, Json]) -> None:
        data = sprite.data
        pivot = data.get("m_Pivot")
        if isinstance(pivot, dict):
            entry["pivotX"] = _number(pivot.get("x"), 0.5)
            entry["pivotY"] = _number(pivot.get("y"), 0.5)
        ppu = _number(data.get("m_PixelsToUnits"), 0.0)
        if ppu > 0:
            entry["pixelsPerUnit"] = ppu
        border = data.get("m_Border")
        if isinstance(border, dict):
            entry["borderLeft"] = _number(border.get("x"), 0.0)
            entry["borderBottom"] = _number(border.get("y"), 0.0)
            entry["borderRight"] = _number(border.get("z"), 0.0)
            entry["borderTop"] = _number(border.get("w"), 0.0)
        texture_id = self._target(sprite.id, "m_RD.texture")
        texture = self._by_id.get(texture_id) if texture_id else None
        if texture is not None:
            self._texture_settings(texture, entry)

    def _texture_settings(self, texture: ObjectRecord, entry: dict[str, Json]) -> None:
        data = texture.data
        settings = data.get("m_TextureSettings")
        if isinstance(settings, dict):
            entry["filterMode"] = int(_number(settings.get("m_FilterMode"), -1))
            entry["wrapU"] = int(_number(settings.get("m_WrapU"), -1))
            entry["wrapV"] = int(_number(settings.get("m_WrapV"), -1))
        entry["mipmaps"] = _number(data.get("m_MipCount"), 1) > 1
        entry["readable"] = bool(data.get("m_IsReadable", False))
        entry["alphaOnly"] = int(_number(data.get("m_TextureFormat"), 0)) == _ALPHA8

    def _material_settings(self, material: ObjectRecord, entry: dict[str, Json]) -> None:
        shader_id = self._target(material.id, "m_Shader")
        shader = self._by_id.get(shader_id) if shader_id else None
        if shader is not None:
            parsed = shader.data.get("m_ParsedForm")
            name = parsed.get("m_Name") if isinstance(parsed, dict) else None
            entry["shaderName"] = name if isinstance(name, str) and name else shader.name
        data = material.data
        entry["renderQueue"] = int(_number(data.get("m_CustomRenderQueue"), -1))
        keywords: list[str] = []
        valid = data.get("m_ValidKeywords")
        if isinstance(valid, list):
            keywords.extend(k for k in valid if isinstance(k, str))
        legacy = data.get("m_ShaderKeywords")
        if isinstance(legacy, str):
            keywords.extend(k for k in legacy.split() if k not in keywords)
        entry["keywords"] = keywords
        props = data.get("m_SavedProperties")
        if not isinstance(props, dict):
            return
        textures: list[dict[str, Json]] = []
        for index, pair in enumerate(props.get("m_TexEnvs") or []):
            if not (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[1], dict)):
                continue
            texture_id = self._target(material.id, f"m_SavedProperties.m_TexEnvs[{index}][1].m_Texture")
            texture = self._by_id.get(texture_id) if texture_id else None
            path = self._asset(texture) if texture is not None else None
            scale = pair[1].get("m_Scale") if isinstance(pair[1].get("m_Scale"), dict) else {}
            offset = pair[1].get("m_Offset") if isinstance(pair[1].get("m_Offset"), dict) else {}
            textures.append({"name": str(pair[0]), "path": path or "",
                             "missing": texture.class_name if texture is not None and path is None else "",
                             "scaleX": _number(scale.get("x"), 1.0), "scaleY": _number(scale.get("y"), 1.0),
                             "offsetX": _number(offset.get("x"), 0.0), "offsetY": _number(offset.get("y"), 0.0)})
        entry["textures"] = textures
        entry["floats"] = [{"name": str(p[0]), "value": _number(p[1], 0.0)} for p in props.get("m_Floats") or []
                           if isinstance(p, list) and len(p) == 2]
        entry["ints"] = [{"name": str(p[0]), "value": _number(p[1], 0.0)} for p in props.get("m_Ints") or []
                         if isinstance(p, list) and len(p) == 2]
        entry["colors"] = [{"name": str(p[0]), "r": _number(p[1].get("r"), 0.0), "g": _number(p[1].get("g"), 0.0),
                            "b": _number(p[1].get("b"), 0.0), "a": _number(p[1].get("a"), 0.0)}
                           for p in props.get("m_Colors") or []
                           if isinstance(p, list) and len(p) == 2 and isinstance(p[1], dict)]

    # -- properties ---------------------------------------------------------------------------

    def _flatten(self, value: Json, unity_path: str, model_path: str, refs: dict[str, Reference],
                 rows: list[dict[str, Json]], skipped: frozenset[str] = _STRUCTURAL) -> None:
        spelling = non_finite(value)
        if spelling is not None:
            # JSON numbers cannot hold NaN/Infinity; the builder parses the spelling from `text`.
            rows.append(_row(unity_path, "number", text=spelling))
        elif isinstance(value, dict):
            if "m_FileID" in value and "m_PathID" in value:
                rows.append(self._reference_row(unity_path, refs.get(model_path)))
                return
            for key, item in value.items():
                if not unity_path and key in skipped:
                    continue
                self._flatten(item, f"{unity_path}.{key}" if unity_path else key,
                              f"{model_path}.{key}" if model_path else key, refs, rows)
        elif isinstance(value, list):
            rows.append(_row(f"{unity_path}.Array.size", "arraySize", number=float(len(value))))
            for index, item in enumerate(value):
                element = f"{unity_path}.Array.data[{index}]"
                if isinstance(item, list) and len(item) == 2:  # map entry: [key, value]
                    self._flatten(item[0], f"{element}.first", f"{model_path}[{index}][0]", refs, rows)
                    self._flatten(item[1], f"{element}.second", f"{model_path}[{index}][1]", refs, rows)
                else:
                    self._flatten(item, element, f"{model_path}[{index}]", refs, rows)
        elif isinstance(value, bool):
            rows.append(_row(unity_path, "number", number=1.0 if value else 0.0))
        elif isinstance(value, (int, float)):
            rows.append(_row(unity_path, "number", number=float(value)))
        elif isinstance(value, str):
            rows.append(_row(unity_path, "string", text=value))

    def _component(self, oid: str) -> dict[str, Json]:
        obj = self._by_id[oid]
        rows: list[dict[str, Json]] = []
        self._flatten(obj.data, "", "", self._refs(oid), rows)
        component: dict[str, Json] = {"id": oid, "className": obj.class_name, "scriptClass": "",
                                      "scriptNamespace": "", "scriptAssembly": "", "rawJson": "", "props": rows}
        if obj.class_name == "MonoBehaviour":
            self._script_identity(obj, component)
            component["rawJson"] = json.dumps(obj.data, ensure_ascii=False, sort_keys=True)
        return component

    # -- hierarchy ----------------------------------------------------------------------------

    def scene(self, name: str, header: dict[str, Json], objects: list[ObjectRecord],
              project: dict[str, Json]) -> dict[str, Json]:
        self._assets = {}
        scene_ids = {o.id for o in objects}
        gameobjects = {o.id: o for o in objects if o.class_name == "GameObject"}
        components: dict[str, list[str]] = {}
        transform_of: dict[str, str] = {}
        for go_id in gameobjects:
            ordered = sorted((r for r in self._edges.get(go_id, ()) if r.field.startswith("m_Component[")),
                             key=lambda r: int(r.field.split("[", 1)[1].split("]", 1)[0]))
            components[go_id] = [r.target_id for r in ordered if r.target_id in scene_ids]
            transform_of[go_id] = next((c for c in components[go_id] if self._by_id[c].class_name in _TRANSFORMS), "")
        # Only GameObjects and their components exist in the rebuilt scene; other scene objects (generated
        # cubemaps, lighting data) are reported as missing when referenced.
        self._linkable = set(gameobjects) | {c for ids in components.values() for c in ids}
        owner = {t: g for g, t in transform_of.items() if t}
        parent_of: dict[str, str] = {}
        children: dict[str, list[str]] = defaultdict(list)
        for go_id, transform in transform_of.items():
            father = self._target(transform, "m_Father") if transform else None
            if father in owner:
                parent_of[go_id] = owner[father]
        for go_id, transform in transform_of.items():
            if not transform:
                continue
            ordered = sorted((r for r in self._edges.get(transform, ()) if r.field.startswith("m_Children[")),
                             key=lambda r: int(r.field.split("[", 1)[1].split("]", 1)[0]))
            children[go_id] = [owner[r.target_id] for r in ordered if r.target_id in owner]
        # A build keeps no editor root order (no SceneRoots object, no Transform.m_RootOrder). Unity loads the roots
        # of a built scene in path-ID order (measured with a probe bundle on 2022.3), so path-ID order is the order
        # the shipped game sees at runtime; `rootOrderBasis` records it.
        roots = sorted((g for g in gameobjects if g not in parent_of), key=lambda g: gameobjects[g].path_id)
        rows: list[dict[str, Json]] = []
        visited: set[str] = set()

        def visit(go_id: str, sibling: int) -> None:
            if go_id in visited:
                raise SceneSpecError(f"GameObject hierarchy has a cycle; scene={name}, object={go_id}")
            visited.add(go_id)
            data = gameobjects[go_id].data
            rows.append({
                "id": go_id, "name": str(data.get("m_Name", gameobjects[go_id].name)),
                "parentId": parent_of.get(go_id, ""), "siblingIndex": sibling,
                "active": bool(data.get("m_IsActive", 1)), "layer": int(data.get("m_Layer", 0) or 0),
                "tag": str(data.get("m_TagString", "Untagged") or "Untagged"),
                "components": [self._component(c) for c in components[go_id]],
            })
            for index, child in enumerate(children.get(go_id, [])):
                visit(child, index)

        for index, root in enumerate(roots):
            visit(root, index)
        orphans = sorted(set(gameobjects) - visited)
        if orphans:
            raise SceneSpecError(f"GameObjects outside the hierarchy; scene={name}, objects={orphans[:3]}")
        settings: list[dict[str, Json]] = []
        for obj in sorted((o for o in objects if o.class_name in _SCENE_SETTINGS), key=lambda o: o.class_name):
            props: list[dict[str, Json]] = []
            self._flatten(obj.data, "", "", self._refs(obj.id), props)
            settings.append({"id": obj.id, "className": obj.class_name, "props": props})
        return {
            "schema": SCHEMA, "version": SCHEMA_VERSION, "scene": name, "scopeId": str(header["scope_id"]),
            "originalPath": str(header.get("name", "")), "source": str(header["source"]),
            "serializedFile": str(header["serialized_file"]), "sourceRoot": str(self._source_root),
            "rootOrderBasis": "path_id",
            "tags": project["tags"], "layers": project["layers"], "sortingLayers": project["sortingLayers"],
            "gameObjects": rows, "sceneSettings": settings,
            "assets": sorted(self._assets.values(), key=lambda a: str(a["path"])),
        }


def _project_settings(model: GameModel) -> dict[str, Json]:
    """Tags, layers and sorting layers of the original build (TagManager), so renderers keep their order."""
    managers = [o for o in model.objects if o.class_name == "TagManager"]
    if len(managers) != 1:
        raise SceneSpecError(f"Expected one TagManager in the game model, found {len(managers)}")
    data = managers[0].data
    tags = [t for t in data.get("tags", []) if isinstance(t, str)]
    layers = [layer if isinstance(layer, str) else "" for layer in data.get("layers", [])]
    sorting = [{"name": str(s.get("name", "")), "uniqueId": int(s.get("uniqueID", 0))}
               for s in data.get("m_SortingLayers", []) if isinstance(s, dict)]
    return {"tags": tags, "layers": layers, "sortingLayers": sorting}


def build_specs(assets_dir: Path, out_dir: Path) -> SceneSpecResult:
    """Write one spec per scene folder of a package output into `out_dir` (must be new or empty)."""
    model_path = assets_dir / "game_model.json"
    scenes_dir = assets_dir / "scenes"
    if not model_path.is_file() or not scenes_dir.is_dir():
        raise SceneSpecError(f"Scene specs need a package output with game_model.json and scenes/; source={assets_dir}")
    model = load_model(model_path)
    builder = _SpecBuilder(model, assets_dir.resolve())
    project = _project_settings(model)
    by_file: dict[tuple[str, str], list[ObjectRecord]] = defaultdict(list)
    for obj in model.objects:
        by_file[obj.source, obj.serialized_file].append(obj)
    names: list[str] = []
    game_objects = components = assets = 0
    with staged_output(out_dir) as staging:
        for scene_dir in sorted(p for p in scenes_dir.iterdir() if p.is_dir()):
            header = json.loads((scene_dir / "scene.json").read_text(encoding="utf-8"))
            missing = [k for k in ("scope_id", "source", "serialized_file") if not isinstance(header.get(k), str)]
            if missing:
                raise SceneSpecError(f"scene.json lacks required fields; scene={scene_dir.name}, fields={missing}")
            spec = builder.scene(scene_dir.name, header,
                                 by_file[header["source"], header["serialized_file"]], project)
            (staging / f"{scene_dir.name}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1),
                                                             encoding="utf-8")
            names.append(scene_dir.name)
            game_objects += len(spec["gameObjects"])
            components += sum(len(g["components"]) for g in spec["gameObjects"])
            assets += len(spec["assets"])
    result = SceneSpecResult(tuple(names), game_objects, components, assets, builder.unresolved, str(out_dir))
    event(LOG, logging.INFO, "scene_specs_written", scenes=len(names), game_objects=game_objects,
          components=components, assets=assets, unresolved=builder.unresolved)
    return result


def summarize(result: SceneSpecResult) -> list[str]:
    return [
        f"scenes            : {len(result.scenes)} specs in {result.out_dir}",
        f"game objects      : {result.game_objects}",
        f"components        : {result.components}",
        f"assets            : {result.assets} asset entries (counted per scene)",
        f"unresolved refs   : {result.unresolved_references}",
    ]
