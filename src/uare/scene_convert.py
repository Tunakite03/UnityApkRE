"""Convert hierarchy JSON (from ``uare package``) to Unity YAML scene and prefab files.

Unity scenes (``.unity``) and prefabs (``.prefab``) use a tagged YAML format where each
document is one serialized object:

    %YAML 1.1
    %TAG !u! tag:unity3d.com,2011:
    --- !u!1 &100000
    GameObject:
      m_Name: Player
      m_Component:
      - component: {fileID: 400000}
      ...
    --- !u!4 &400000
    Transform:
      m_GameObject: {fileID: 100000}
      ...

This module reads the hierarchy JSON produced by ``_build_hierarchy`` in ``package_tree.py``
and generates Unity YAML that can be opened in the Unity Editor (with warnings about missing
scripts, but the structure, names and transforms are preserved).
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.scene_convert")

_YAML_HEADER = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
"""

_CLASS_IDS: dict[str, int] = {
    "GameObject": 1, "Component": 2, "Transform": 4, "Camera": 20,
    "Material": 21, "MeshRenderer": 23, "Texture2D": 28, "MeshFilter": 33,
    "Renderer": 25, "ParticleSystem": 198, "Rigidbody": 54,
    "Collider": 56, "BoxCollider": 65, "SphereCollider": 135,
    "CapsuleCollider": 136, "MeshCollider": 64, "CharacterController": 143,
    "Light": 108, "Behaviour": 8, "MonoBehaviour": 114,
    "MonoScript": 115, "Animator": 95, "Animation": 111,
    "AudioSource": 82, "AudioListener": 81, "Canvas": 223,
    "CanvasRenderer": 222, "RectTransform": 224, "CanvasGroup": 225,
    "SpriteRenderer": 212, "Sprite": 213, "LineRenderer": 120,
    "TrailRenderer": 96, "SkinnedMeshRenderer": 137, "Rigidbody2D": 50,
    "BoxCollider2D": 61, "CircleCollider2D": 58, "PolygonCollider2D": 60,
    "TextMesh": 102, "ParticleSystemRenderer": 199,
    "LODGroup": 205, "ReflectionProbe": 215, "Terrain": 218,
    "EventSystem": 114, "Image": 114, "Text": 114, "Button": 114,
    "ScrollRect": 114, "InputField": 114, "Slider": 114,
    "Dropdown": 114, "Toggle": 114,
}

_SCENE_PREAMBLE = """\
--- !u!29 &1
OcclusionCullingSettings:
  m_ObjectHideFlags: 0
  serializedVersion: 2
  m_OcclusionBakeSettings:
    smallestOccluder: 5
    smallestHole: 0.25
    backfaceThreshold: 100
  m_SceneGUID: 00000000000000000000000000000000
  m_OcclusionCullingData: {fileID: 0}
--- !u!104 &2
RenderSettings:
  m_ObjectHideFlags: 0
  serializedVersion: 9
  m_Fog: 0
  m_FogColor: {r: 0.5, g: 0.5, b: 0.5, a: 1}
  m_FogMode: 3
  m_FogDensity: 0.01
  m_LinearFogStart: 0
  m_LinearFogEnd: 300
  m_AmbientSkyColor: {r: 0.212, g: 0.227, b: 0.259, a: 1}
  m_AmbientEquatorColor: {r: 0.114, g: 0.125, b: 0.133, a: 1}
  m_AmbientGroundColor: {r: 0.047, g: 0.043, b: 0.035, a: 1}
  m_AmbientIntensity: 1
  m_AmbientMode: 0
  m_SubtractiveShadowColor: {r: 0.42, g: 0.478, b: 0.627, a: 1}
  m_SkyboxMaterial: {fileID: 0}
  m_HaloStrength: 0.5
  m_FlareStrength: 1
  m_FlareFadeSpeed: 3
  m_HaloTexture: {fileID: 0}
  m_SpotCookie: {fileID: 0}
  m_DefaultReflectionMode: 0
  m_DefaultReflectionResolution: 128
  m_ReflectionBounces: 1
  m_ReflectionIntensity: 1
  m_CustomReflection: {fileID: 0}
  m_Sun: {fileID: 0}
  m_UseRadianceAmbientProbe: 0
--- !u!157 &3
LightmapSettings:
  m_ObjectHideFlags: 0
  serializedVersion: 12
  m_GIWorkflowMode: 1
  m_EnableBakedLightmaps: 0
  m_EnableRealtimeLightmaps: 0
--- !u!196 &4
NavMeshSettings:
  m_ObjectHideFlags: 0
  serializedVersion: 2
  m_BuildSettings:
    serializedVersion: 3
    agentTypeID: 0
    agentRadius: 0.5
    agentHeight: 2
    agentSlope: 45
    agentClimb: 0.4
    ledgeDropHeight: 0
    maxJumpAcrossDistance: 0
    minRegionArea: 2
    manualCellSize: 0
    cellSize: 0.16666667
    manualTileSize: 0
    tileSize: 256
    buildHeightMesh: 0
  m_NavMeshData: {fileID: 0}
"""


@dataclass(frozen=True)
class ConvertResult:
    scenes_converted: int
    prefabs_converted: int
    total_gameobjects: int
    total_components: int
    errors: int
    out_dir: str


class _IdAllocator:
    def __init__(self, base: int = 100000) -> None:
        self._next = base

    def alloc(self) -> int:
        fid = self._next
        self._next += 1
        return fid


def _yaml_value(value: Any, indent: int = 0) -> str:
    prefix = "  " * indent
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if any(c in value for c in ":{}\n#[]"):
            return json.dumps(value)
        return value
    if isinstance(value, dict):
        if len(value) <= 5 and all(isinstance(v, (int, float, str, bool, type(None))) for v in value.values()):
            pairs = ", ".join(f"{k}: {_yaml_value(v)}" for k, v in value.items())
            return "{" + pairs + "}"
        lines = []
        for k, v in value.items():
            lines.append(f"{prefix}  {k}: {_yaml_value(v, indent + 1)}")
        return "\n" + "\n".join(lines)
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        lines = []
        for item in value:
            lines.append(f"{prefix}  - {_yaml_value(item, indent + 1)}")
        return "\n" + "\n".join(lines)
    return str(value)


def _class_id(class_name: str) -> int:
    return _CLASS_IDS.get(class_name, 114)


def _serialize_gameobject(name: str, go_fid: int, component_fids: list[int],
                          layer: int = 0, active: bool = True) -> str:
    comp_list = "\n".join(f"  - component: {{fileID: {fid}}}" for fid in component_fids)
    return f"""\
--- !u!1 &{go_fid}
GameObject:
  m_ObjectHideFlags: 0
  m_CorrespondingSourceObject: {{fileID: 0}}
  m_PrefabInstance: {{fileID: 0}}
  m_PrefabAsset: {{fileID: 0}}
  serializedVersion: 6
  m_Component:
{comp_list}
  m_Layer: {layer}
  m_Name: {name}
  m_TagString: Untagged
  m_Icon: {{fileID: 0}}
  m_NavMeshLayer: 0
  m_StaticEditorFlags: 0
  m_IsActive: {1 if active else 0}
"""


def _serialize_transform(transform_fid: int, go_fid: int, parent_fid: int,
                         children_fids: list[int], data: dict | None) -> str:
    pos = data.get("m_LocalPosition", {}) if data else {}
    rot = data.get("m_LocalRotation", {}) if data else {}
    scale = data.get("m_LocalScale", {}) if data else {}
    px = pos.get("x", 0)
    py = pos.get("y", 0)
    pz = pos.get("z", 0)
    rx = rot.get("x", 0)
    ry = rot.get("y", 0)
    rz = rot.get("z", 0)
    rw = rot.get("w", 1)
    sx = scale.get("x", 1)
    sy = scale.get("y", 1)
    sz = scale.get("z", 1)
    children_list = "\n".join(f"  - {{fileID: {fid}}}" for fid in children_fids)
    if not children_list:
        children_list = "  []"
    else:
        children_list = "\n" + children_list
    return f"""\
--- !u!4 &{transform_fid}
Transform:
  m_ObjectHideFlags: 0
  m_CorrespondingSourceObject: {{fileID: 0}}
  m_PrefabInstance: {{fileID: 0}}
  m_PrefabAsset: {{fileID: 0}}
  m_GameObject: {{fileID: {go_fid}}}
  serializedVersion: 2
  m_LocalRotation: {{x: {rx}, y: {ry}, z: {rz}, w: {rw}}}
  m_LocalPosition: {{x: {px}, y: {py}, z: {pz}}}
  m_LocalScale: {{x: {sx}, y: {sy}, z: {sz}}}
  m_ConstrainProportionsScale: 0
  m_Children: {children_list}
  m_Father: {{fileID: {parent_fid}}}
  m_LocalEulerAnglesHint: {{x: 0, y: 0, z: 0}}
"""


def _serialize_monobehaviour(fid: int, go_fid: int, script_class: str,
                             data: dict | None) -> str:
    lines = [
        f"--- !u!114 &{fid}",
        "MonoBehaviour:",
        "  m_ObjectHideFlags: 0",
        f"  m_CorrespondingSourceObject: {{fileID: 0}}",
        f"  m_PrefabInstance: {{fileID: 0}}",
        f"  m_PrefabAsset: {{fileID: 0}}",
        f"  m_GameObject: {{fileID: {go_fid}}}",
        "  m_Enabled: 1",
        "  m_EditorHideFlags: 0",
        f"  m_Script: {{fileID: 0}}",
        f"  m_Name: {script_class}",
    ]
    if data and isinstance(data, dict):
        for key, value in data.items():
            if key.startswith("m_") and key not in ("m_GameObject", "m_Script", "m_Enabled",
                                                     "m_ObjectHideFlags", "m_Name"):
                lines.append(f"  {key}: {_yaml_value(value, 1)}")
    return "\n".join(lines) + "\n"


def _serialize_generic_component(cls: str, fid: int, go_fid: int, data: dict | None) -> str:
    class_id = _class_id(cls)
    lines = [
        f"--- !u!{class_id} &{fid}",
        f"{cls}:",
        "  m_ObjectHideFlags: 0",
        f"  m_CorrespondingSourceObject: {{fileID: 0}}",
        f"  m_PrefabInstance: {{fileID: 0}}",
        f"  m_PrefabAsset: {{fileID: 0}}",
        f"  m_GameObject: {{fileID: {go_fid}}}",
    ]
    if data and isinstance(data, dict):
        for key, value in data.items():
            if key.startswith("m_") and key not in ("m_GameObject", "m_ObjectHideFlags"):
                lines.append(f"  {key}: {_yaml_value(value, 1)}")
    return "\n".join(lines) + "\n"


def _convert_node(node: dict, parent_transform_fid: int, ids: _IdAllocator,
                  documents: list[str], go_count: list[int], comp_count: list[int]) -> int:
    """Convert one hierarchy node and its children. Returns the transform fileID."""
    go_fid = ids.alloc()
    transform_fid = ids.alloc()
    go_count[0] += 1

    component_fids = [transform_fid]
    component_docs: list[str] = []
    transform_data = None

    for comp in node.get("components", []):
        cls = comp.get("class", "")
        data = comp.get("data")
        if cls in ("Transform", "RectTransform"):
            transform_data = data
            continue
        comp_fid = ids.alloc()
        component_fids.append(comp_fid)
        comp_count[0] += 1
        if cls == "MonoBehaviour":
            script = comp.get("script", "")
            component_docs.append(_serialize_monobehaviour(comp_fid, go_fid, script, data))
        else:
            component_docs.append(_serialize_generic_component(cls, comp_fid, go_fid, data))

    children = node.get("children", [])
    child_transform_fids: list[int] = []
    for child in children:
        child_tf = _convert_node(child, transform_fid, ids, documents, go_count, comp_count)
        child_transform_fids.append(child_tf)

    documents.append(_serialize_gameobject(node.get("name", ""), go_fid, component_fids))
    documents.append(_serialize_transform(transform_fid, go_fid, parent_transform_fid,
                                          child_transform_fids, transform_data))
    documents.extend(component_docs)
    return transform_fid


def convert_hierarchy(hierarchy: dict, out_path: Path, is_scene: bool = True) -> tuple[int, int]:
    """Convert one hierarchy JSON to a .unity or .prefab file. Returns (go_count, comp_count)."""
    ids = _IdAllocator()
    documents: list[str] = []
    go_count = [0]
    comp_count = [0]

    for root in hierarchy.get("roots", []):
        _convert_node(root, 0, ids, documents, go_count, comp_count)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        handle.write(_YAML_HEADER)
        if is_scene:
            handle.write(_SCENE_PREAMBLE)
        for doc in documents:
            handle.write(doc)
    return go_count[0], comp_count[0]


def convert_scenes_and_prefabs(source_dir: Path, out_dir: Path) -> ConvertResult:
    """Convert all hierarchy JSONs from a ``uare package`` output directory."""
    if not source_dir.is_dir():
        raise UareError(f"{source_dir} is not a directory")

    scene_dir = source_dir / "scenes"
    prefab_dir = source_dir / "assets" / "prefabs"
    total_go = total_comp = scenes = prefabs = errors = 0

    with staged_output(out_dir) as staging:
        if scene_dir.is_dir():
            for scene_path in sorted(scene_dir.rglob("hierarchy.json")):
                try:
                    hierarchy = json.loads(scene_path.read_text(encoding="utf-8"))
                    scene_name = scene_path.parent.name
                    out_path = staging / "Scenes" / f"{scene_name}.unity"
                    go, comp = convert_hierarchy(hierarchy, out_path, is_scene=True)
                    total_go += go
                    total_comp += comp
                    scenes += 1
                    event(LOG, logging.INFO, "scene_converted", name=scene_name, gameobjects=go)
                except Exception as exc:
                    errors += 1
                    event(LOG, logging.WARNING, "scene_convert_failed",
                          path=str(scene_path), error=str(exc))

        if prefab_dir.is_dir():
            for prefab_path in sorted(prefab_dir.rglob("*.json")):
                try:
                    hierarchy = json.loads(prefab_path.read_text(encoding="utf-8"))
                    prefab_name = prefab_path.stem
                    group = prefab_path.parent.name
                    out_path = staging / "Prefabs" / group / f"{prefab_name}.prefab"
                    go, comp = convert_hierarchy(hierarchy, out_path, is_scene=False)
                    total_go += go
                    total_comp += comp
                    prefabs += 1
                except Exception as exc:
                    errors += 1
                    event(LOG, logging.WARNING, "prefab_convert_failed",
                          path=str(prefab_path), error=str(exc))

        (staging / "convert_report.json").write_text(
            json.dumps({"scenes": scenes, "prefabs": prefabs, "gameobjects": total_go,
                        "components": total_comp, "errors": errors},
                       indent=2), encoding="utf-8")

    event(LOG, logging.INFO, "scene_convert_done", scenes=scenes, prefabs=prefabs,
          gameobjects=total_go, components=total_comp, errors=errors)
    return ConvertResult(scenes, prefabs, total_go, total_comp, errors, str(out_dir))


def summarize(result: ConvertResult) -> list[str]:
    return [
        f"scenes converted  : {result.scenes_converted}",
        f"prefabs converted : {result.prefabs_converted}",
        f"gameobjects       : {result.total_gameobjects}",
        f"components        : {result.total_components}",
        f"errors            : {result.errors}",
        f"output            : {result.out_dir}",
    ]
