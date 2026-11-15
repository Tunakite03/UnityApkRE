"""Scene-oriented reconstruction reports derived only from recorded evidence."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from uare.addressables import AddressableAssetPaths, asset_paths as catalog_asset_paths
from uare.game_model import (GameModel, Json, ModelError, ObjectRecord, Reference, array,
                            closure, identity, load_model, outgoing, read_json, record, save_model,
                            string, write_json)
from uare.outdir import staged_output

ANIMATION_CLASSES = frozenset({"Animator", "Animation", "AnimationClip", "AnimatorController",
                               "AnimatorOverrideController", "Avatar", "AvatarMask"})
SETTINGS_CLASSES = frozenset({"RenderSettings", "LightmapSettings", "NavMeshSettings", "OcclusionCullingSettings"})
_LEVEL = re.compile(r"^level(\d+)$")
_GUID_FILE = re.compile(r"[0-9a-f]{32}")
# Public asset GUIDs of Unity packages, with the object name each asset contains. When a build stores
# these assets in files named by the GUID, its serialized files are named by source asset GUID.
_GUID_ANCHORS = {
    "8f586378b4e144a9851e7b34d9b748ee": "LiberationSans SDF",  # TextMeshPro default font asset
    "f952c082cb03451daed3ee968ac6c63e": "Default Style Sheet",  # TextMeshPro default style sheet
}
GUID_FILE_EVIDENCE = "serialized file named by source asset GUID (verified by package GUID anchors)"
CATALOG_PREFAB_EVIDENCE = "AssetBundle.m_Container GUID + Addressables catalog path"
CATALOG_SCENE_EVIDENCE = "AssetBundle.m_SceneHashes GUID + Addressables catalog path"


@dataclass(frozen=True)
class BlueprintResult:
    objects: int
    scenes: int
    prefabs: int
    unresolved_references: int
    out_dir: str


def _target(edges: dict[str, tuple[Reference, ...]], oid: str, field: str) -> str | None:
    return next((r.target_id for r in edges.get(oid, ()) if r.field == field), None)


def _strings(value: Json, path: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        return [entry for key, item in value.items() for entry in _strings(item, f"{path}.{key}")]
    if isinstance(value, list):
        return [entry for i, item in enumerate(value) for entry in _strings(item, f"{path}[{i}]")]
    return []


def _bundle_assets(model: GameModel, edges: dict[str, tuple[Reference, ...]]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = defaultdict(list)
    for obj in model.objects:
        if obj.class_name != "AssetBundle":
            continue
        container = obj.data.get("m_Container", [])
        if not isinstance(container, list):
            continue
        for i, pair in enumerate(container):
            if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                continue
            target = _target(edges, obj.id, f"m_Container[{i}][1].asset")
            if target:
                aliases[target].append(pair[0])
    return dict(aliases)


def container_keys(model: GameModel, edges: dict[str, tuple[Reference, ...]]) -> dict[str, list[str]]:
    """Object id -> every AssetBundle container key that names it (paths, or asset GUIDs in GUID-named builds)."""
    return _bundle_assets(model, edges)


def _hierarchy(ids: set[str], by_id: dict[str, ObjectRecord],
               edges: dict[str, tuple[Reference, ...]]) -> tuple[list[Json], list[str]]:
    transforms = {oid for oid in ids if by_id[oid].class_name in {"Transform", "RectTransform"}}
    parents = {oid: _target(edges, oid, "m_Father") for oid in transforms}
    children: dict[str, list[str]] = defaultdict(list)
    for oid, parent in sorted(parents.items()):
        if parent in transforms:
            children[parent].append(oid)
    for parent, kids in children.items():
        # Numeric array index, not lexical ordering (index 10 must follow index 9).
        order = [r.target_id for r in sorted(
            (r for r in edges.get(parent, ()) if r.field.startswith("m_Children[")),
            key=lambda r: int(r.field.split("[")[1].split("]")[0]))]
        rank = {oid: index for index, oid in enumerate(order)}
        kids.sort(key=lambda oid: (rank.get(oid, len(rank)), oid))
    visited: set[str] = set()
    seen_go: set[str] = set()
    issues: list[str] = []

    def node(tid: str, depth: int) -> Json:
        if tid in visited or depth > 256:
            issues.append(f"Hierarchy cycle or depth limit at {tid}")
            return {"id": tid, "issue": "cycle_or_depth_limit"}
        visited.add(tid)
        goid = _target(edges, tid, "m_GameObject")
        go = by_id.get(goid or "")
        if go is None:
            issues.append(f"Transform has unresolved GameObject: {tid}")
            return {"id": tid, "issue": "unresolved_gameobject"}
        seen_go.add(go.id)
        comps = [r for r in edges.get(go.id, ()) if r.field.startswith("m_Component[")]
        return {"id": go.id, "name": go.name, "gameobject": go.path_id,
                "transform_id": tid, "data": go.data,
                "components": [{"id": r.target_id, "field": r.field, "status": r.status,
                                 "class": by_id[r.target_id].class_name if r.target_id else None}
                                for r in comps],
                "children": [node(child, depth + 1) for child in children.get(tid, [])]}

    roots = [node(tid, 0) for tid in sorted(transforms) if parents[tid] not in transforms]
    for tid in sorted(transforms - visited):
        if tid not in visited:
            issues.append(f"Detached or cyclic hierarchy: {tid}")
            roots.append(node(tid, 0))
    for oid in sorted(ids - seen_go):
        if by_id[oid].class_name == "GameObject":
            roots.append({"id": oid, "name": by_id[oid].name, "data": by_id[oid].data,
                          "issue": "no_resolved_transform", "children": []})
            issues.append(f"GameObject has no resolved Transform: {oid}")
    return roots, issues


def _structural_ids(root: str, by_id: dict[str, ObjectRecord],
                    edges: dict[str, tuple[Reference, ...]]) -> set[str]:
    pending = [root]
    visited: set[str] = set()
    while pending:
        oid = pending.pop()
        if oid in visited:
            continue
        visited.add(oid)
        cls = by_id[oid].class_name
        for ref in edges.get(oid, ()):
            if ref.target_id and ((cls == "GameObject" and ref.field.startswith("m_Component[")) or
                                 (cls in {"Transform", "RectTransform"} and
                                  (ref.field == "m_GameObject" or ref.field.startswith("m_Children[")))):
                pending.append(ref.target_id)
    return visited


@dataclass(frozen=True)
class ScopeSpec:
    """A scene, prefab or unclassified GameObject tree, before any report or export is built."""
    kind: str
    name: str
    source: str
    serialized_file: str
    object_ids: frozenset[str]
    root_id: str | None
    build_index: int | None
    evidence: str

    @property
    def scope_id(self) -> str:
        seed = min(self.object_ids) if self.object_ids else identity(self.source, self.serialized_file, 0)
        return seed + "-" + self.kind


def guid_named_layout(model: GameModel) -> bool:
    """True when every package anchor present in the build sits in a file named by its GUID."""
    names: dict[str, set[str]] = defaultdict(set)
    for obj in model.objects:
        if obj.serialized_file in _GUID_ANCHORS:
            names[obj.serialized_file].add(obj.name)
    return bool(names) and all(_GUID_ANCHORS[file] in found for file, found in names.items())


def _root_game_objects(ids: set[str], by_id: dict[str, ObjectRecord],
                       edges: dict[str, tuple[Reference, ...]]) -> list[str]:
    transforms = {oid for oid in ids if by_id[oid].class_name in {"Transform", "RectTransform"}}
    roots = (_target(edges, oid, "m_GameObject") for oid in sorted(transforms)
             if _target(edges, oid, "m_Father") not in transforms)
    return [oid for oid in roots if oid in ids]


def build_scene_paths(model: GameModel) -> list[str]:
    """Scene paths of the player's BuildSettings, in build-index order (levelN is entry N).

    Unity stores the list as `m_Scenes` in older type trees and as `scenes` in newer ones (2022.3).
    An empty list means no single BuildSettings object with a list of path strings exists.
    """
    lists = [value for o in model.objects if o.class_name == "BuildSettings"
             for value in (o.data.get("m_Scenes", o.data.get("scenes")),) if isinstance(value, list)]
    if len(lists) != 1 or not all(isinstance(item, str) for item in lists[0]):
        return []
    return list(lists[0])


def original_asset_paths(model: GameModel, edges: dict[str, tuple[Reference, ...]],
                         asset_paths: Mapping[str, str] | None = None) -> dict[str, str]:
    """Object id -> original project path, from AssetBundle containers: a key that is itself an `Assets/` path,
    or an asset GUID the Addressables catalog names. Objects claimed by several different paths are left out."""
    claims: dict[str, set[str]] = defaultdict(set)
    for oid, keys in _bundle_assets(model, edges).items():
        for key in keys:
            path = key if key.startswith("Assets/") else (asset_paths or {}).get(key)
            if path:
                claims[oid].add(path)
    return {oid: next(iter(paths)) for oid, paths in claims.items() if len(paths) == 1}


def _catalog_aliases(aliases: dict[str, list[str]], asset_paths: Mapping[str, str]) -> dict[str, list[str]]:
    """Container keys that are asset GUIDs, replaced by the path an Addressables catalog records."""
    return {oid: [asset_paths[key] for key in keys if key in asset_paths]
            for oid, keys in aliases.items() if any(key in asset_paths for key in keys)}


def classify_scopes(model: GameModel, by_id: dict[str, ObjectRecord],
                    edges: dict[str, tuple[Reference, ...]],
                    asset_paths: Mapping[str, str] | None = None) -> list[ScopeSpec]:
    """Scenes, prefabs and unclassified trees. `asset_paths` (asset GUID -> path from an Addressables
    catalog) names container entries and streamed scenes whose keys are GUIDs; it never overrides
    a path the bundle itself records."""
    aliases = _bundle_assets(model, edges)
    catalog_aliases = _catalog_aliases(aliases, asset_paths or {})
    guid_layout = guid_named_layout(model)
    scene_hashes: dict[tuple[str, str], set[str]] = defaultdict(set)
    catalog_scenes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for obj in model.objects:
        if obj.class_name != "AssetBundle" or obj.data.get("m_IsStreamedSceneAssetBundle") is not True:
            continue
        entries = obj.data.get("m_SceneHashes", [])
        if not isinstance(entries, list):
            continue
        for pair in entries:
            if (isinstance(pair, list) and len(pair) == 2 and
                    isinstance(pair[0], str) and pair[0].endswith(".unity") and isinstance(pair[1], str)):
                # Streamed scenes have null container PPtrs. Unity records their
                # serialized file identities separately, scoped to this bundle.
                scene_hashes[obj.source, pair[1]].add(pair[0])
            elif (asset_paths and isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str) and
                    isinstance(pair[1], str) and asset_paths.get(pair[0], "").endswith(".unity")):
                catalog_scenes[obj.source, pair[1]].add(asset_paths[pair[0]])
    build_order = build_scene_paths(model)
    specs: list[ScopeSpec] = []
    by_file: dict[tuple[str, str], set[str]] = defaultdict(set)
    for obj in model.objects:
        by_file[obj.source, obj.serialized_file].add(obj.id)
    for file in model.files:
        ids = by_file[file.source, file.serialized_file]
        match = _LEVEL.fullmatch(file.serialized_file)
        hashed_scenes = scene_hashes.get((file.source, file.serialized_file), set())
        own_scenes = sorted(hashed_scenes | {
            alias for oid in ids for alias in aliases.get(oid, []) if alias.endswith(".unity")})
        catalog_named = [] if own_scenes else sorted(catalog_scenes.get((file.source, file.serialized_file), set()))
        scene_aliases = own_scenes or catalog_named
        if match or scene_aliases or file.serialized_file.endswith(".unity"):
            index = int(match[1]) if match else None
            name = scene_aliases[0] if scene_aliases else file.serialized_file
            evidence = ("AssetBundle.m_SceneHashes" if hashed_scenes else
                        "asset_container" if own_scenes else
                        CATALOG_SCENE_EVIDENCE if catalog_named else "serialized_filename")
            if index is not None and index < len(build_order):
                name, evidence = build_order[index], "BuildSettings.m_Scenes + serialized_filename"
            specs.append(ScopeSpec("scene", name, file.source, file.serialized_file, frozenset(ids), None,
                                   index, evidence))
        elif any(by_id[oid].class_name == "GameObject" for oid in ids):
            prefab_names: dict[str, tuple[str, str]] = {}
            for oid in ids:
                if by_id[oid].class_name != "GameObject":
                    continue
                own = next((a for a in aliases.get(oid, []) if a.endswith(".prefab")), None)
                listed = next((a for a in catalog_aliases.get(oid, []) if a.endswith(".prefab")), None)
                if own:
                    prefab_names[oid] = (own, "AssetBundle.m_Container")
                elif listed:
                    prefab_names[oid] = (listed, CATALOG_PREFAB_EVIDENCE)
            prefab_roots = sorted(prefab_names)
            covered: set[str] = set()
            for oid in prefab_roots:
                members = _structural_ids(oid, by_id, edges)
                covered.update(members)
                name, evidence = prefab_names[oid]
                specs.append(ScopeSpec("prefab", name, file.source, file.serialized_file, frozenset(members),
                                       oid, None, evidence))
            remaining = ids - covered
            roots = _root_game_objects(remaining, by_id, edges)
            if guid_layout and not prefab_roots and _GUID_FILE.fullmatch(file.serialized_file) and len(roots) == 1:
                # One asset per file: a single-root hierarchy is that asset, a prefab with this GUID.
                members = _structural_ids(roots[0], by_id, edges)
                specs.append(ScopeSpec("prefab", by_id[roots[0]].name or file.serialized_file, file.source,
                                       file.serialized_file, frozenset(members), roots[0], None,
                                       GUID_FILE_EVIDENCE))
                remaining -= members
            if any(by_id[oid].class_name == "GameObject" for oid in remaining):
                specs.append(ScopeSpec("unclassified_hierarchy", file.serialized_file, file.source,
                                       file.serialized_file, frozenset(remaining), None, None,
                                       "serialized_objects; prefab provenance unavailable"))
    return specs


def scene_reports(model: GameModel, asset_paths: Mapping[str, str] | None = None) -> list[dict[str, Json]]:
    by_id = {o.id: o for o in model.objects}
    edges = outgoing(model)
    reports: list[dict[str, Json]] = []
    for spec in classify_scopes(model, by_id, edges, asset_paths):
        ids = set(spec.object_ids)
        reachable = closure(ids, edges)
        direct = {r.target_id for oid in ids for r in edges.get(oid, ()) if r.target_id and r.target_id not in ids}
        unresolved = [asdict(r) for oid in sorted(reachable) for r in edges.get(oid, ())
                      if r.status not in {"resolved", "null"}]
        roots, issues = _hierarchy(ids, by_id, edges)
        script_bindings: list[Json] = []
        events: list[Json] = []
        for oid in sorted(reachable):
            obj = by_id[oid]
            if obj.class_name == "MonoBehaviour":
                script = _target(edges, oid, "m_Script")
                script_bindings.append({"component_id": oid, "script_id": script,
                                        "status": "resolved" if script else "unresolved"})
            for field, value in _strings(obj.data, ""):
                if field.endswith((".m_MethodName", ".functionName")) and value:
                    events.append({"object_id": oid, "field": field, "method": value,
                                   "evidence": "serialized_event", "execution": "not_observed"})
        reports.append(record({"id": spec.scope_id, "kind": spec.kind, "name": spec.name, "source": spec.source,
                               "build_index_candidate": spec.build_index, "classification_evidence": spec.evidence,
                               "object_ids": sorted(ids), "roots": roots,
                               "direct_assets": sorted(direct), "transitive_assets": sorted(reachable - ids - direct),
                               "animation_objects": sorted(oid for oid in reachable if by_id[oid].class_name in ANIMATION_CLASSES),
                               "scene_settings": sorted(oid for oid in ids if by_id[oid].class_name in SETTINGS_CLASSES),
                               "script_bindings": script_bindings, "events": events,
                               "unresolved_references": unresolved, "hierarchy_issues": issues,
                               "logic": {"status": "unknown", "reason": "Serialized events and fields do not establish method bodies or the core loop."},
                               "dynamic_loading": "not_observed", "play_mode": "not_tested"}, spec.scope_id))
    return reports

# These signatures identify presence, never a package version or runtime usage.
_SIGNATURES: tuple[tuple[str, str, str | None, tuple[str, ...]], ...] = (
    ("Addressables", "unity_package", "com.unity.addressables", ("unity.addressables", "unityengine.addressableassets")),
    ("Universal Render Pipeline", "unity_package", "com.unity.render-pipelines.universal", ("unity.renderpipelines.universal", "unityengine.rendering.universal")),
    ("TextMeshPro", "unity_package", "com.unity.textmeshpro", ("unity.textmeshpro", "tmpro.")),
    ("Input System", "unity_package", "com.unity.inputsystem", ("unity.inputsystem", "unityengine.inputsystem")),
    ("Unity UI", "unity_package", "com.unity.ugui", ("unityengine.ui.", "unityengine.eventsystems.")),
    ("Unity IAP", "unity_package", "com.unity.purchasing", ("unityengine.purchasing", "unity.purchasing")),
    ("Firebase", "service_sdk", None, ("firebase.", "libfirebase")),
    ("PlayFab", "service_sdk", None, ("playfab.",)),
    ("Photon", "network_sdk", None, ("photon.", "photon3unity3d", "exitgames.")),
    ("AdMob", "service_sdk", None, ("googlemobileads", "com.google.android.gms.ads")),
    ("DOTween", "embedded_plugin", None, ("dg.tweening", "dotween")),
    ("Spine", "embedded_plugin", None, ("spine.", "spine-unity")),
    ("FMOD", "native_plugin", None, ("fmod",)),
    ("Wwise", "native_plugin", None, ("ak.wwise", "akaudio", "wwise")),
    ("YooAsset", "embedded_plugin", None, ("yooasset",)),
    ("HybridCLR", "embedded_plugin", None, ("hybridclr",)),
)


def dependencies(model: GameModel, workdir: Path) -> list[Json]:
    evidence: list[tuple[str, str]] = []
    for obj in model.objects:
        if obj.class_name == "MonoScript":
            value = ".".join(str(obj.data.get(k, "")) for k in ("m_AssemblyName", "m_Namespace", "m_ClassName"))
            evidence.append((obj.id, value))
    extracted = workdir / "extracted"
    if extracted.is_dir():
        evidence.extend((p.relative_to(workdir).as_posix(), p.name) for p in sorted(extracted.rglob("*"))
                        if p.is_file() and p.suffix.lower() in {".dll", ".so", ".aar", ".jar"})
    for table in (workdir / "il2cpp" / "types.tsv", workdir / "analysis" / "il2cpp" / "identifiers.tsv"):
        if table.is_file():
            with table.open(encoding="utf-8") as handle:
                for line, row in enumerate(csv.reader(handle, delimiter="\t"), 1):
                    if row:
                        evidence.append((f"{table.relative_to(workdir).as_posix()}:{line}", ".".join(row[1:])))
    result: list[Json] = []
    for name, kind, package_id, patterns in _SIGNATURES:
        hits = sorted({source for source, text in evidence if any(pattern in text.lower() for pattern in patterns)})
        if hits:
            result.append({"name": name, "kind": kind, "package_id_candidate": package_id,
                           "original_version": None, "rebuild_version": None, "status": "detected",
                           "basis": "name_signature", "runtime_usage": "not_verified", "evidence": hits})
    return result


def runtime_observations(path: Path, scope_ids: set[str]) -> list[Json]:
    if not path.exists():
        return []
    raw = read_json(path)
    result: list[Json] = []
    for value in array(raw.get("observations"), "observations"):
        row = record(value, "observation")
        scope = string(row.get("scope_id"), "scope_id")
        if scope != "game" and scope not in scope_ids:
            raise ModelError("Runtime observation refers to an unknown scope")
        result.append({"scope_id": scope, "action": string(row.get("action"), "action"),
                       "result": string(row.get("result"), "result"),
                       "evidence": string(row.get("evidence"), "evidence"),
                       "basis": "supplied_runtime_observation", "independently_verified": False})
    return result


def behavior_claims(workdir: Path, scope_ids: set[str]) -> list[Json]:
    """Accept agent-authored behavior analysis only with locally verifiable citations."""
    path = workdir / "behavior.json"
    if not path.is_file():
        return []
    claims: list[Json] = []
    for value in array(read_json(path).get("claims"), "claims"):
        claim = record(value, "behavior claim")
        scope = string(claim.get("scope_id"), "scope_id")
        basis = string(claim.get("basis"), "basis")
        if (scope != "game" and scope not in scope_ids) or basis not in {"code_analysis", "inference"}:
            raise ModelError("Behavior claim has an invalid scope or evidence basis")
        citations: list[Json] = []
        for item in array(claim.get("citations"), "citations"):
            citation = record(item, "citation")
            relative = string(citation.get("path"), "citation path")
            target = (workdir / relative).resolve()
            if not target.is_relative_to(workdir.resolve()) or not target.is_file():
                raise ModelError("Behavior citation must reference an existing file inside the work directory")
            start, end = citation.get("start_line"), citation.get("end_line")
            if type(start) is not int or type(end) is not int or start < 1 or end < start:
                raise ModelError("Behavior citation has an invalid line range")
            try:
                lines = target.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                raise ModelError("Behavior citation is not readable text") from exc
            if end > len(lines):
                raise ModelError("Behavior citation exceeds the source file")
            digest = hashlib.sha256("\n".join(lines[start - 1:end]).encode("utf-8")).hexdigest()
            citations.append({"path": relative, "start_line": start, "end_line": end, "sha256": digest})
        if not citations:
            raise ModelError("Behavior claims require at least one source citation")
        claims.append({"scope_id": scope, "system": string(claim.get("system"), "system"),
                       "statement": string(claim.get("statement"), "statement"), "basis": basis,
                       "citations": citations, "reconstruction": "not_verified"})
    return claims


def script_inventory(model: GameModel, workdir: Path) -> list[Json]:
    """Connect serialized scripts to metadata members without inventing executable logic."""
    types: dict[str, list[tuple[str, str]]] = defaultdict(list)
    members: dict[tuple[str, str], list[Json]] = defaultdict(list)
    for directory in (workdir / "il2cpp", workdir / "analysis" / "il2cpp"):
        table = directory / "types.tsv"
        member_table = directory / "members.tsv"
        if not table.is_file():
            continue
        with table.open(encoding="utf-8") as handle:
            for row in csv.reader(handle, delimiter="\t"):
                if len(row) >= 3:
                    fullname = f"{row[1]}.{row[2]}" if row[1] else row[2]
                    types[fullname].append((str(directory), row[0]))
        if member_table.is_file():
            with member_table.open(encoding="utf-8") as handle:
                for line, row in enumerate(csv.reader(handle, delimiter="\t"), 1):
                    if len(row) >= 5:
                        members[str(directory), row[0]].append({"kind": row[2], "name": row[4],
                            "evidence": f"{member_table.relative_to(workdir).as_posix()}:{line}", "body_status": "not_analyzed"})
    result: list[Json] = []
    for obj in model.objects:
        if obj.class_name != "MonoScript":
            continue
        cls, namespace = obj.data.get("m_ClassName", ""), obj.data.get("m_Namespace", "")
        fullname = f"{namespace}.{cls}" if namespace else str(cls)
        matches = types.get(fullname, [])
        result.append({"object_id": obj.id, "type": fullname, "assembly": obj.data.get("m_AssemblyName"),
                       "metadata_match": "unique_name_candidate" if len(matches) == 1 else "ambiguous" if matches else "absent",
                       "members": members.get(matches[0], []) if len(matches) == 1 else [],
                       "behavior": "not_analyzed"})
    return result


def library_inventory(model: GameModel, workdir: Path) -> list[Json]:
    """Keep unrecognized assemblies/plugins visible alongside known SDK signatures."""
    libraries: dict[tuple[str, str], set[str]] = defaultdict(set)
    for obj in model.objects:
        assembly = obj.data.get("m_AssemblyName") if obj.class_name == "MonoScript" else None
        if isinstance(assembly, str) and assembly:
            libraries["managed_assembly", assembly].add(obj.id)
    root = workdir / "extracted"
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".dll", ".so", ".aar", ".jar"}:
                kind = "managed_assembly" if path.suffix.lower() == ".dll" else "native_or_android_plugin"
                libraries[kind, path.name].add(path.relative_to(workdir).as_posix())
    return [{"kind": kind, "name": name, "evidence": sorted(sources),
             "classification": "requires_review", "original_version": None}
            for (kind, name), sources in sorted(libraries.items())]


def build_report(model: GameModel, workdir: Path,
                 scopes: list[dict[str, Json]] | None = None) -> dict[str, Json]:
    scopes = scene_reports(model) if scopes is None else scopes
    statuses = Counter(ref.status for ref in model.references)
    data_statuses = Counter(obj.data_status for obj in model.objects)
    analysis_path = workdir / "analysis" / "report.json"
    analysis = read_json(analysis_path) if analysis_path.exists() else {}
    inventory = record(analysis.get("inventory", {}), "inventory")
    scope_ids = {str(s["id"]) for s in scopes}
    return record({"schema": "uare.reconstruction-blueprint", "schema_version": 1,
                   "evidence_root": str(workdir.resolve()),
                   "engine": {"unity_versions": inventory.get("unity_versions", {}),
                              "scripting_backend": analysis.get("scripting_backend", "unknown")},
                   "coverage": {"files": len(model.files), "objects": len(model.objects),
                                "object_data": dict(data_statuses), "references": dict(statuses),
                                "denominator": "objects and serialized references in the supplied readable files",
                                "remote_content": "not_verified", "runtime": "not_verified"},
                   "scopes": [scope_summary(scope) for scope in scopes], "dependencies": dependencies(model, workdir),
                   "script_inventory": script_inventory(model, workdir),
                   "library_inventory": library_inventory(model, workdir),
                   "behavior_claims": behavior_claims(workdir, scope_ids),
                   "observations": runtime_observations(workdir / "observations.json", scope_ids),
                   "blockers": list(model.issues),
                   "limitations": ["Missing type trees can hide references; resolved-reference counts are not total game coverage.",
                                   "Scene filename indices are candidates until checked against build settings and runtime loading.",
                                   "Prefab authoring links, variants and overrides may not survive the build.",
                                   "Animation data is preserved; optimized controller semantics and rig compatibility need verification.",
                                   "Dynamic loading, remote content, method bodies, core loop and backend behavior require further evidence.",
                                   "SDK signatures show presence, not initialization, exact versions or service availability."],
                   "validation": {"unity_import": "not_tested", "compilation": "not_tested", "play_mode": "not_tested"}}, "blueprint")


def scope_summary(scope: dict[str, Json]) -> dict[str, Json]:
    """The part of a scope that report.json keeps; the full scope is written to scopes/<id>.js."""
    return {"id": scope["id"], "kind": scope["kind"], "name": scope["name"], "source": scope["source"],
            "classification_evidence": scope["classification_evidence"],
            "object_count": len(array(scope["object_ids"], "object_ids")),
            "unresolved_count": len(array(scope["unresolved_references"], "unresolved_references"))}


def _markdown(report: dict[str, Json]) -> str:
    lines = ["# Game reconstruction blueprint", "", "## Evidence coverage", "", "```json",
             json.dumps(report["coverage"], indent=2), "```", "", "## Scenes and prefab evidence", "",
             "Full scope detail (hierarchy, assets, references) is in `scopes/<id>.js`.", ""]
    for item in array(report["scopes"], "scopes"):
        scope = record(item, "scope")
        sid = string(scope["id"], "scope.id")
        # JSON quoting prevents untrusted names from becoming Markdown headings or HTML.
        name = html.escape(json.dumps(scope["name"], ensure_ascii=False))
        lines.extend([f"- {name} ({scope['kind']}): [detail](scopes/{sid}.js)",
                      f"  - Objects: {scope['object_count']}; unresolved references: "
                      f"{scope['unresolved_count']}. Logic: unknown; Play Mode: not tested."])
    lines.extend(["", "## Dependencies", ""])
    for dep in array(report["dependencies"], "dependencies"):
        row = record(dep, "dependency")
        lines.append(f"- {row['name']}: detected by signature; original version unknown; runtime usage unverified.")
    lines.extend(["", "## Reconstruction limits", ""])
    lines.extend(f"- {html.escape(str(item))}" for item in array(report["limitations"], "limitations"))
    lines.extend(["", "## Behavior analysis", ""])
    claims = array(report["behavior_claims"], "behavior_claims")
    if not claims:
        lines.append("Core loop, scene transitions, progression, saving and service behavior remain unknown.")
    for claim in claims:
        row = record(claim, "claim")
        lines.append(f"- {html.escape(str(row['system']))}: {html.escape(str(row['statement']))} ({row['basis']}).")
        for item in array(row["citations"], "citations"):
            citation = record(item, "citation")
            lines.append(f"  - Evidence: {html.escape(str(citation['path']))}, lines {citation['start_line']}-{citation['end_line']}.")
    lines.extend(["", "## Validation", "", "Unity import, compilation and Play Mode have not been tested.", ""])
    return "\n".join(lines)


def _js_payload(value: object) -> str:
    return json.dumps(value, ensure_ascii=True).replace("<", "\\u003c")


def _write_object_chunks(staging: Path, model: GameModel) -> None:
    """One script per id prefix: object metadata and serialized fields, loaded on demand by index.html."""
    chunks: dict[str, dict[str, dict[str, Json]]] = defaultdict(dict)
    for obj in model.objects:
        chunks[obj.id[:2]][obj.id] = {
            "class_name": obj.class_name, "name": obj.name, "source": obj.source,
            "serialized_file": obj.serialized_file, "path_id": obj.path_id, "data_status": obj.data_status,
            "issues": list(obj.issues), "output": obj.output, "data": obj.data}
    directory = staging / "objects"
    directory.mkdir()
    for prefix, entries in sorted(chunks.items()):
        (directory / f"{prefix}.js").write_text(f"window.uareReceiveObjects({_js_payload(entries)});\n", encoding="utf-8")


_PAGE_REPORT_KEYS = ("schema", "schema_version", "engine", "coverage", "scopes", "dependencies",
                     "behavior_claims", "observations", "blockers", "limitations", "validation")


def _catalog_record(workdir: Path, catalog: AddressableAssetPaths) -> dict[str, Json]:
    path = Path(catalog.source).resolve()
    try:
        shown = path.relative_to(workdir.resolve()).as_posix()
    except ValueError:
        shown = path.as_posix()
    return {"path": shown, "sha256": catalog.sha256, "asset_paths": len(catalog.paths),
            "ambiguous": catalog.ambiguous}


def write_blueprint(workdir: Path, out_dir: Path, model: GameModel,
                    catalog: AddressableAssetPaths | None = None) -> BlueprintResult:
    scopes = scene_reports(model, catalog.paths if catalog else None)
    report = build_report(model, workdir, scopes)
    if catalog:
        report["addressables_catalog"] = _catalog_record(workdir, catalog)
    with staged_output(out_dir) as staging:
        write_json(staging / "report.json", report)
        (staging / "scopes").mkdir()
        for scope in scopes:
            (staging / "scopes" / f"{scope['id']}.js").write_text(
                f"window.uareReceiveScope({_js_payload(scope)});\n", encoding="utf-8")
        (staging / "overview.md").write_text(_markdown(report), encoding="utf-8")
        _write_object_chunks(staging, model)
        # The page shows only summaries; full script and library inventories stay in report.json.
        page_report = {key: report[key] for key in _PAGE_REPORT_KEYS}
        (staging / "index.html").write_text(_HTML.replace("__PAYLOAD__", _js_payload({"report": page_report})),
                                            encoding="utf-8")
    return BlueprintResult(len(model.objects), sum(s["kind"] == "scene" for s in scopes),
                           sum(s["kind"] == "prefab" for s in scopes),
                           sum(r.status not in {"resolved", "null"} for r in model.references), str(out_dir))


def blueprint(workdir: Path, out_dir: Path, assets_dir: Path,
              addressables_catalog: Path | None = None) -> BlueprintResult:
    source = assets_dir / "game_model.json"
    if not source.is_file():
        raise ModelError(f"Blueprint needs scoped object data; rerun `uare package` with this version; source={source}")
    # Parse the catalog first: a malformed catalog fails before the multi-gigabyte model is loaded.
    catalog = catalog_asset_paths(addressables_catalog) if addressables_catalog else None
    return write_blueprint(workdir, out_dir, load_model(source), catalog)


_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Game reconstruction blueprint</title>
<style>body{font:15px/1.5 system-ui;margin:0;color:#e5e9f0;background:#111827}header{padding:20px 28px;border-bottom:1px solid #374151}main{display:grid;grid-template-columns:280px minmax(0,1fr)}nav,article{padding:20px;overflow:auto;max-height:calc(100vh - 90px)}nav{border-right:1px solid #374151}button{display:block;width:100%;text-align:left;margin:4px 0;padding:6px;color:inherit;background:#1f2937;border:1px solid #374151;cursor:pointer}input{width:100%;box-sizing:border-box;margin:8px 0;padding:6px;background:#0b1220;color:inherit;border:1px solid #374151}pre{white-space:pre-wrap;word-break:break-word;background:#0b1220;padding:12px}@media (max-width:760px){main{grid-template-columns:1fr}nav{border-right:0}}</style>
<header><h1>Game reconstruction blueprint</h1><small>Serialized evidence · Unknown behavior stays unknown · Unity validation pending</small></header>
<main><nav aria-label="Game navigation"><button id="overview">Overview</button><label for="search">Find scene or prefab</label><input id="search" type="search"><label for="object-id">Open object by id</label><input id="object-id" type="text" autocomplete="off"><div id="scopes"></div></nav><article id="detail" aria-live="polite"></article></main>
<script id="data" type="application/json">__PAYLOAD__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('data').textContent),report=data.report;
const scopeDetail=new Map(),objectEntries=new Map(),loaded=new Set();
window.uareReceiveScope=s=>{scopeDetail.set(s.id,s)};
window.uareReceiveObjects=entries=>{for(const [id,e] of Object.entries(entries))objectEntries.set(id,e)};
function loadScript(src){if(loaded.has(src))return Promise.resolve();return new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=src;s.onload=()=>{loaded.add(src);resolve()};s.onerror=()=>reject(new Error('Cannot load '+src+'. Keep index.html next to the scopes and objects folders.'));document.head.append(s)})}
function loadScope(id){return loadScript('scopes/'+id+'.js').then(()=>{if(!scopeDetail.has(id))throw new Error('Scope data is missing: '+id);return scopeDetail.get(id)})}
function loadObject(id){if(objectEntries.has(id))return Promise.resolve(objectEntries.get(id));return loadScript('objects/'+id.slice(0,2)+'.js').then(()=>{if(!objectEntries.has(id))throw new Error('Object is missing from its chunk: '+id);return objectEntries.get(id)})}
const detail=document.getElementById('detail');
function el(tag,text,parent){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;parent.append(e);return e}
function show(title){detail.replaceChildren();el('h2',title,detail)}
function pre(value,parent){el('pre',JSON.stringify(value,null,2),parent)}
function showError(e){show('Error');el('p',e.message,detail)}
function objectButton(id,label,parent){const b=el('button',label,parent);b.onclick=()=>object(id).catch(showError);return b}
function list(label,items,parent){el('h3',label+' ('+items.length+')',parent);pre(items.slice(0,200),parent);if(items.length>200)el('p','Showing the first 200 entries.',parent)}
function tree(nodes,parent){for(const n of nodes||[]){const d=el('details','',parent);el('summary',n.name||n.id,d);objectButton(n.id,'Open object '+String(n.id).slice(0,12),d);for(const c of n.components||[]){if(c.id)objectButton(c.id,'Component '+String(c.id).slice(0,12),d);else el('p','Component: '+c.status,d)}tree(n.children,d)}}
function object(id){return loadObject(id).then(o=>{show(o.name||o.class_name);el('p',o.source+' / '+o.serialized_file+' #'+o.path_id,detail);el('p','Extraction: '+o.data_status,detail);for(const issue of o.issues)el('p',issue,detail);if(o.output)el('p','Exported: '+o.output,detail);el('h3','Serialized fields',detail);pre(o.data,detail)})}
function scope(id){return loadScope(id).then(s=>{show(s.name);el('p',s.kind+' · '+s.classification_evidence,detail);el('h3','Hierarchy and components',detail);tree(s.roots,detail);list('Direct assets',s.direct_assets,detail);list('Transitive assets',s.transitive_assets,detail);list('Unresolved references',s.unresolved_references,detail);list('Hierarchy issues',s.hierarchy_issues,detail);list('Script bindings',s.script_bindings,detail);list('Events (not executed)',s.events,detail);el('h3','Logic',detail);pre(s.logic,detail)})}
function overview(){show('Reconstruction readiness');el('p','The supplied files describe '+report.coverage.objects+' objects in '+report.coverage.files+' serialized files. Coverage does not include undiscovered or remote content.',detail);el('h3','Limitations',detail);for(const l of report.limitations)el('p',l,detail);el('h3','Validation',detail);pre(report.validation,detail);el('h3','Coverage',detail);pre(report.coverage,detail)}
const nav=document.getElementById('scopes');
function renderNav(filter){nav.replaceChildren();const q=filter.toLowerCase();let shown=0;for(const s of report.scopes){if(q&&!(s.name+' '+s.kind).toLowerCase().includes(q))continue;if(shown>=300)break;shown++;const b=el('button',s.name+' · '+s.kind+' · '+s.object_count+' objects',nav);b.onclick=()=>scope(s.id).catch(showError)}el('p',shown+' shown (at most 300). Type in the search box to narrow the list.',nav)}
renderNav('');
document.getElementById('overview').onclick=overview;
document.getElementById('search').oninput=e=>renderNav(e.target.value);
document.getElementById('object-id').onkeydown=e=>{if(e.key==='Enter'&&e.target.value.trim())object(e.target.value.trim()).catch(showError)};
const initial=location.hash.slice(1);
if(initial.startsWith('scope/'))scope(initial.slice(6)).catch(showError);
else if(initial.startsWith('object/'))object(initial.slice(7)).catch(showError);
else overview();
</script></html>"""
