"""Generate structural Unity scenes from validated model objects and explicit bindings.

Unsupported assets remain evidence. Every non-null reference that cannot be
represented in the generated project is recorded as a reconstruction blocker.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from uare.asset_import import guid_for
from uare.blueprint import SETTINGS_CLASSES, scene_reports
from uare.game_model import GameModel, Json, ObjectRecord, array, record, write_json
from uare.scene_convert import _CLASS_IDS, _YAML_HEADER, _SCENE_PREAMBLE


@dataclass(frozen=True)
class Binding:
    path: str
    file_id: int
    guid: str
    type: int


@dataclass(frozen=True)
class ExportResult:
    scenes: int
    prefabs: int
    bindings: dict[str, Binding]
    blockers: tuple[dict[str, Json], ...]


def _yaml(value: Json, indent: int) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            encoded = key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else json.dumps(key)
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{prefix}{encoded}:")
                lines.extend(_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}{encoded}: {_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                nested = _yaml(item, indent + 2)
                lines.append(prefix + "- " + nested[0].lstrip())
                lines.extend(nested[1:])
            else:
                lines.append(f"{prefix}- {_scalar(item)}")
        return lines
    return [prefix + _scalar(value)]


def _scalar(value: Json) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _script_bindings(model: GameModel, project: Path) -> dict[str, Binding]:
    sources = list((project / "Assets" / "Scripts").rglob("*.cs"))
    source_index: dict[tuple[str, str], list[Path]] = {}
    for path in sources:
        text = path.read_text(encoding="utf-8")
        namespaces = re.findall(r"\bnamespace\s+([\w.]+)\s*[{;]", text)
        if re.search(r"\bclass\s+" + re.escape(path.stem) + r"\b", text):
            for namespace in namespaces or [""]:
                source_index.setdefault((namespace, path.stem), []).append(path)
    assembly_index: dict[tuple[str, str], set[str]] = {}
    for obj in model.objects:
        if obj.class_name == "MonoScript":
            key = (str(obj.data.get("m_Namespace", "")), str(obj.data.get("m_ClassName", "")))
            assembly_index.setdefault(key, set()).add(str(obj.data.get("m_AssemblyName")))
    bindings: dict[str, Binding] = {}
    for obj in model.objects:
        if obj.class_name != "MonoScript":
            continue
        cls, namespace = obj.data.get("m_ClassName"), obj.data.get("m_Namespace")
        if not isinstance(cls, str) or not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z_]\w*", cls):
            continue
        matches = source_index.get((namespace, cls), [])
        # Namespace/name matches cannot disambiguate identical types in different assemblies.
        assemblies = assembly_index[namespace, cls]
        if len(matches) == 1 and len(assemblies) == 1:
            rel = matches[0].relative_to(project / "Assets").as_posix()
            bindings[obj.id] = Binding("Assets/" + rel, 11500000, guid_for(rel), 3)
    return bindings


def export_project(model: GameModel, project: Path, media_bindings: dict[str, Binding]) -> ExportResult:
    by_id = {o.id: o for o in model.objects}
    edge_fields = {(r.source_id, r.field): r for r in model.references}
    scopes = [s for s in scene_reports(model) if s["kind"] in {"scene", "prefab"}]
    bindings = {**media_bindings, **_script_bindings(model, project)}
    blockers: list[dict[str, Json]] = []
    plans: list[tuple[dict[str, Json], str, dict[str, int]]] = []
    component_ids = {r.target_id for r in model.references if r.field.startswith("m_Component[") and r.target_id}
    for scope in scopes:
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(str(scope["name"])).stem)[:80] or "Recovered"
        extension = ".unity" if scope["kind"] == "scene" else ".prefab"
        folder = "Scenes" if scope["kind"] == "scene" else "Prefabs"
        path = f"Assets/{folder}/{name}_{str(scope['id'])[:12]}{extension}"
        ids = [str(oid) for oid in array(scope["object_ids"], "object_ids")]
        supported = [oid for oid in ids if by_id[oid].class_name in _CLASS_IDS and
                     (oid in component_ids or by_id[oid].class_name == "GameObject")]
        supported += [oid for oid in ids if by_id[oid].class_name in SETTINGS_CLASSES]
        local = {oid: i + 100000 for i, oid in enumerate(sorted(set(supported)))}
        plans.append((scope, path, local))
        # Scene objects are not asset references across scene files. Prefab objects are.
        if scope["kind"] == "prefab":
            for oid, fid in local.items():
                bindings[oid] = Binding(path, fid, guid_for(path.removeprefix("Assets/")), 3)
        for oid in ids:
            if oid not in local:
                blockers.append({"scope_id": scope["id"], "object_id": oid,
                                 "reason": "Object type is not supported by the structural scene exporter"})

    def rewrite(value: Json, obj: ObjectRecord, field: str, local: dict[str, int]) -> Json:
        if isinstance(value, dict):
            if "m_FileID" in value and "m_PathID" in value:
                ref = edge_fields.get((obj.id, field))
                if ref is not None and ref.status == "null":
                    return {"fileID": 0}
                if ref and ref.target_id in local:
                    return {"fileID": local[ref.target_id]}
                if ref and ref.target_id in bindings:
                    binding = bindings[ref.target_id]
                    return {"fileID": binding.file_id, "guid": binding.guid, "type": binding.type}
                blockers.append({"object_id": obj.id, "field": field, "target_id": ref.target_id if ref else None,
                                 "reason": ref.status if ref and ref.status != "resolved" else "Target has no verified Unity export binding"})
                return {"fileID": 0}
            if "$binary_base64" in value:
                blockers.append({"object_id": obj.id, "field": field, "reason": "Binary field needs a class-specific exporter"})
                return None
            return {key: rewrite(item, obj, f"{field}.{key}" if field else key, local) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item, obj, f"{field}[{i}]", local) for i, item in enumerate(value)]
        return value

    for scope, path, local in plans:
        documents = [_YAML_HEADER]
        if scope["kind"] == "scene" and not any(by_id[oid].class_name in SETTINGS_CLASSES for oid in local):
            documents.append(_SCENE_PREAMBLE)
            blockers.append({"scope_id": scope["id"], "reason": "Scene settings unavailable; generated default settings"})
        for oid, fid in local.items():
            obj = by_id[oid]
            if obj.data_status != "complete":
                blockers.append({"object_id": oid, "reason": "Serialized fields only partially recovered"})
            fields = record(rewrite(obj.data, obj, "", local), oid)
            if obj.class_name == "MonoBehaviour" and "m_Script" not in fields:
                fields["m_Script"] = {"fileID": 0}
                blockers.append({"object_id": oid, "field": "m_Script", "reason": "Script field unavailable"})
            class_id = _CLASS_IDS.get(obj.class_name, {"RenderSettings": 104, "LightmapSettings": 157,
                                    "NavMeshSettings": 196, "OcclusionCullingSettings": 29}.get(obj.class_name))
            documents.append(f"--- !u!{class_id} &{fid}\n{obj.class_name}:\n" + "\n".join(_yaml(fields, 2)) + "\n")
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(documents), encoding="utf-8")
    for obj in model.objects:
        if obj.class_name in {"AnimationClip", "AnimatorController", "AnimatorOverrideController", "Mesh", "Material", "Shader"}:
            if obj.id not in bindings:
                blockers.append({"object_id": obj.id, "reason": "Evidence preserved; native Unity asset reconstruction is not verified"})
    result = ExportResult(sum(s["kind"] == "scene" for s in scopes), sum(s["kind"] == "prefab" for s in scopes), bindings, tuple(blockers))
    write_json(project / "Reconstruction" / "bindings.json", {key: asdict(value) for key, value in bindings.items()})
    write_json(project / "Reconstruction" / "export_blockers.json", blockers)
    return result
