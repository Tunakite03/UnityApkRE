"""Scene-oriented reconstruction reports derived only from recorded evidence."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from uare.game_model import (GameModel, Json, ModelError, ObjectRecord, Reference, array,
                            closure, identity, load_model, outgoing, read_json, record, save_model,
                            string, write_json)
from uare.outdir import staged_output

ANIMATION_CLASSES = frozenset({"Animator", "Animation", "AnimationClip", "AnimatorController",
                               "AnimatorOverrideController", "Avatar", "AvatarMask"})
SETTINGS_CLASSES = frozenset({"RenderSettings", "LightmapSettings", "NavMeshSettings", "OcclusionCullingSettings"})
_LEVEL = re.compile(r"^level(\d+)$")


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


def classify_scopes(model: GameModel, by_id: dict[str, ObjectRecord],
                    edges: dict[str, tuple[Reference, ...]]) -> list[ScopeSpec]:
    aliases = _bundle_assets(model, edges)
    scene_hashes: dict[tuple[str, str], set[str]] = defaultdict(set)
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
    build_orders = [o for o in model.objects if o.class_name == "BuildSettings" and
                    isinstance(o.data.get("m_Scenes"), list)]
    build_order = build_orders[0].data["m_Scenes"] if len(build_orders) == 1 else []
    specs: list[ScopeSpec] = []
    by_file: dict[tuple[str, str], set[str]] = defaultdict(set)
    for obj in model.objects:
        by_file[obj.source, obj.serialized_file].add(obj.id)
    for file in model.files:
        ids = by_file[file.source, file.serialized_file]
        match = _LEVEL.fullmatch(file.serialized_file)
        hashed_scenes = scene_hashes.get((file.source, file.serialized_file), set())
        scene_aliases = sorted(hashed_scenes | {
            alias for oid in ids for alias in aliases.get(oid, []) if alias.endswith(".unity")})
        if match or scene_aliases or file.serialized_file.endswith(".unity"):
            index = int(match[1]) if match else None
            name = scene_aliases[0] if scene_aliases else file.serialized_file
            evidence = ("AssetBundle.m_SceneHashes" if hashed_scenes else
                        "asset_container" if scene_aliases else "serialized_filename")
            if index is not None and isinstance(build_order, list) and index < len(build_order):
                item = build_order[index]
                if isinstance(item, str):
                    name, evidence = item, "BuildSettings.m_Scenes + serialized_filename"
            specs.append(ScopeSpec("scene", name, file.source, file.serialized_file, frozenset(ids), None,
                                   index, evidence))
        elif any(by_id[oid].class_name == "GameObject" for oid in ids):
            prefab_roots = [oid for oid in ids if by_id[oid].class_name == "GameObject" and
                            any(a.endswith(".prefab") for a in aliases.get(oid, []))]
            covered: set[str] = set()
            for oid in sorted(prefab_roots):
                members = _structural_ids(oid, by_id, edges)
                covered.update(members)
                specs.append(ScopeSpec("prefab", next(a for a in aliases[oid] if a.endswith(".prefab")),
                                       file.source, file.serialized_file, frozenset(members), oid, None,
                                       "AssetBundle.m_Container"))
            remaining = ids - covered
            if any(by_id[oid].class_name == "GameObject" for oid in remaining):
                specs.append(ScopeSpec("unclassified_hierarchy", file.serialized_file, file.source,
                                       file.serialized_file, frozenset(remaining), None, None,
                                       "serialized_objects; prefab provenance unavailable"))
    return specs


def scene_reports(model: GameModel) -> list[dict[str, Json]]:
    by_id = {o.id: o for o in model.objects}
    edges = outgoing(model)
    reports: list[dict[str, Json]] = []
    for spec in classify_scopes(model, by_id, edges):
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


def build_report(model: GameModel, workdir: Path) -> dict[str, Json]:
    scopes = scene_reports(model)
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
                   "scopes": scopes, "dependencies": dependencies(model, workdir),
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


def _markdown(report: dict[str, Json]) -> str:
    lines = ["# Game reconstruction blueprint", "", "## Evidence coverage", "", "```json",
             json.dumps(report["coverage"], indent=2), "```", "", "## Scenes and prefab evidence", ""]
    for item in array(report["scopes"], "scopes"):
        scope = record(item, "scope")
        sid = string(scope["id"], "scope.id")
        # JSON quoting prevents untrusted names from becoming Markdown headings or HTML.
        name = html.escape(json.dumps(scope["name"], ensure_ascii=False))
        lines.extend([f"- {name} ({scope['kind']}): [manifest](scopes/{sid}.json)",
                      f"  - Objects: {len(array(scope['object_ids'], 'ids'))}; unresolved references: "
                      f"{len(array(scope['unresolved_references'], 'refs'))}. Logic: unknown; Play Mode: not tested."])
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


def _write_object_chunks(staging: Path, model: GameModel) -> list[dict[str, object]]:
    chunks: dict[str, dict[str, dict[str, Json]]] = defaultdict(dict)
    summaries: list[dict[str, object]] = []
    for obj in model.objects:
        chunks[obj.id[:2]][obj.id] = obj.data
        summaries.append(asdict(replace(obj, data=obj.data if obj.class_name == "MonoScript" else {})))
    directory = staging / "objects"
    directory.mkdir()
    for prefix, fields in sorted(chunks.items()):
        payload = json.dumps(fields, ensure_ascii=True).replace("<", "\\u003c")
        (directory / f"{prefix}.js").write_text(f"window.uareReceiveObjects({payload});\n", encoding="utf-8")
    return summaries


def write_blueprint(workdir: Path, out_dir: Path, model: GameModel) -> BlueprintResult:
    report = build_report(model, workdir)
    with staged_output(out_dir) as staging:
        save_model(staging / "game_model.json", model)
        write_json(staging / "report.json", report)
        for scope in array(report["scopes"], "scopes"):
            row = record(scope, "scope")
            write_json(staging / "scopes" / f"{row['id']}.json", row)
        (staging / "overview.md").write_text(_markdown(report), encoding="utf-8")
        summaries = _write_object_chunks(staging, model)
        payload = json.dumps({"report": report, "objects": summaries,
                              "references": [asdict(r) for r in model.references]}, ensure_ascii=True).replace("<", "\\u003c")
        (staging / "index.html").write_text(_HTML.replace("__PAYLOAD__", payload), encoding="utf-8")
    scopes = [record(s, "scope") for s in array(report["scopes"], "scopes")]
    return BlueprintResult(len(model.objects), sum(s["kind"] == "scene" for s in scopes),
                           sum(s["kind"] == "prefab" for s in scopes),
                           sum(r.status not in {"resolved", "null"} for r in model.references), str(out_dir))


def blueprint(workdir: Path, out_dir: Path, assets_dir: Path) -> BlueprintResult:
    source = assets_dir / "game_model.json"
    if not source.is_file():
        raise ModelError(f"Blueprint needs scoped object data; rerun `uare package` with this version; source={source}")
    return write_blueprint(workdir, out_dir, load_model(source))


_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Game reconstruction blueprint</title>
<style>body{font:15px/1.5 system-ui;margin:0;color:#e5e9f0;background:#111827}header{padding:20px 28px;border-bottom:1px solid #374151}main{display:grid;grid-template-columns:280px minmax(0,1fr)}nav,article{padding:20px;overflow:auto}nav{border-right:1px solid #374151}button,input{font:inherit;padding:8px;background:#1f2937;color:inherit;border:1px solid #64748b;border-radius:4px}button{cursor:pointer;text-align:left;margin:3px}nav button{display:block;width:100%}button:focus-visible,input:focus-visible{outline:3px solid #38bdf8}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#1f2937;padding:12px}small{color:#a9b8cc}details{margin:8px 0}h1{margin:0} @media(max-width:700px){main{grid-template-columns:1fr}nav{border-bottom:1px solid #374151}}</style>
<header><h1>Game reconstruction blueprint</h1><small>Serialized evidence · Unknown behavior stays unknown · Unity validation pending</small></header>
<main><nav aria-label="Game navigation"><button id="overview">Overview</button><label for="search">Find scene, asset or script</label><input id="search" type="search"><div id="scopes"></div><div id="results"></div></nav><article id="detail" aria-live="polite"></article></main>
<script id="data" type="application/json">__PAYLOAD__</script><script>
'use strict';
const data=JSON.parse(document.getElementById('data').textContent),report=data.report;
const objects=new Map(data.objects.map(o=>[o.id,o])),out=new Map(),used=new Map();
const objectFields=new Map(),chunks=new Map();
window.uareReceiveObjects=entries=>{for(const [id,fields]of Object.entries(entries))objectFields.set(id,fields)};
function loadFields(id){const prefix=id.slice(0,2);if(!chunks.has(prefix)){chunks.set(prefix,new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='objects/'+prefix+'.js';script.onload=resolve;script.onerror=()=>{chunks.delete(prefix);reject(new Error('Cannot load object data. Keep index.html and the objects folder together.'))};document.head.append(script)}))}return chunks.get(prefix).then(()=>{if(!objectFields.has(id))throw new Error('Object data is missing from its chunk.');return objectFields.get(id)})}
for(const r of data.references){if(!out.has(r.source_id))out.set(r.source_id,[]);out.get(r.source_id).push(r);if(r.target_id){if(!used.has(r.target_id))used.set(r.target_id,[]);used.get(r.target_id).push(r)}}
const detail=document.getElementById('detail');
function element(tag,text,parent){const e=document.createElement(tag);e.textContent=text;parent.append(e);return e}
function show(title){detail.replaceChildren();element('h2',title,detail)}
function json(value,parent){element('pre',JSON.stringify(value,null,2),parent)}
function link(id,parent,prefix=''){const o=objects.get(id);if(!o){element('p',prefix+'Unresolved object',parent);return}const b=element('button',prefix+(o.name||o.class_name)+' · '+o.class_name,parent);b.onclick=()=>object(id)}
function object(id){const o=objects.get(id);show(o.name||o.class_name);element('p',o.source+' / '+o.serialized_file+' #'+o.path_id,detail);element('p','Extraction: '+o.data_status,detail);for(const issue of o.issues)element('p',issue,detail);const fields=element('details','',detail);element('summary','Serialized fields (load on demand)',fields);let loaded=false;fields.ontoggle=()=>{if(fields.open&&!loaded){loaded=true;const status=element('p','Loading fields…',fields);loadFields(id).then(value=>{status.remove();json(value,fields)}).catch(error=>{status.textContent=error.message;loaded=false})}};const script=report.script_inventory.find(s=>s.object_id===id);if(script){element('h3','Script metadata and members',detail);json(script,detail)}element('h3','References',detail);for(const r of out.get(id)||[]){if(r.target_id)link(r.target_id,detail,r.field+' → ');else element('p',r.field+': '+r.status,detail)}element('h3','Used by',detail);for(const r of used.get(id)||[])link(r.source_id,detail,r.field+' ← ')}
function tree(nodes,parent){for(const n of nodes){const d=element('details','',parent);element('summary',n.name||n.id,d);link(n.id,d);for(const c of n.components||[]){if(c.id)link(c.id,d);else element('p','Component: '+c.status,d)}tree(n.children||[],d)}}
function scope(s){show(s.name);element('p',s.kind+' · '+s.classification_evidence,detail);element('h3','Hierarchy and components',detail);tree(s.roots,detail);for(const [label,key]of [['Direct assets','direct_assets'],['Transitive assets','transitive_assets'],['Animations','animation_objects'],['Scene settings','scene_settings']]){element('h3',label,detail);for(const id of s[key])link(id,detail)}element('h3','Behavior analysis',detail);const claims=report.behavior_claims.filter(c=>c.scope_id===s.id);if(!claims.length)element('p','No verified behavior analysis yet. Inspect script fields, events and code evidence.',detail);for(const c of claims){element('p',c.system+': '+c.statement+' ['+c.basis+']',detail);json(c.citations,detail)}element('h3','Script bindings, events and missing evidence',detail);json({script_bindings:s.script_bindings,events:s.events,logic:s.logic,dynamic_loading:s.dynamic_loading,unresolved_references:s.unresolved_references,hierarchy_issues:s.hierarchy_issues},detail)}
function overview(){show('Reconstruction readiness');element('p','The supplied files describe '+report.coverage.objects+' objects in '+report.coverage.files+' serialized files. Coverage does not include undiscovered or remote content.',detail);element('h3','Serialized references',detail);const counts=element('ul','',detail);for(const [key,value] of Object.entries(report.coverage.references))element('li',key+': '+value,counts);element('h3','Object recovery',detail);const states=element('ul','',detail);for(const [key,value] of Object.entries(report.coverage.object_data))element('li',key+': '+value,states);element('h3','Engine',detail);element('p','Backend: '+report.engine.scripting_backend+' · Unity versions: '+Object.keys(report.engine.unity_versions).join(', '),detail);element('h3','Packages and services',detail);if(!report.dependencies.length)element('p','No supported signatures found in the supplied evidence. This does not prove that dependencies are absent.',detail);for(const dep of report.dependencies){const row=element('details','',detail);element('summary',dep.name+' · '+dep.kind+' · version unknown',row);element('p','Runtime usage unverified. Evidence:',row);for(const source of dep.evidence){if(objects.has(source))link(source,row);else element('p',source,row)}}const libraries=element('details','',detail);element('summary','All managed and native libraries, including unclassified dependencies',libraries);for(const item of report.library_inventory)element('p',item.name+' · '+item.kind+' · '+item.classification,libraries);element('h3','Core game and system analysis',detail);const claims=report.behavior_claims.filter(c=>c.scope_id==='game');if(!claims.length)element('p','Core loop, progression, saving and backend behavior have not been established. Add analysis with source citations.',detail);for(const c of claims){element('p',c.system+': '+c.statement+' ['+c.basis+']',detail);json(c.citations,detail)}element('h3','Observed behavior',detail);for(const o of report.observations)element('p',o.action+' → '+o.result+' · '+o.evidence,detail);if(!report.observations.length)element('p','No runtime observations supplied.',detail);element('h3','Missing evidence and limits',detail);const limits=element('ul','',detail);for(const item of [...report.blockers,...report.limitations])element('li',item,limits);element('h3','Validation',detail);element('p','Unity import: not tested · Compilation: not tested · Play Mode: not tested',detail)}
const nav=document.getElementById('scopes');for(const s of report.scopes){const b=element('button',s.name+' · '+s.kind,nav);b.onclick=()=>scope(s)}
document.getElementById('overview').onclick=overview;
document.getElementById('search').oninput=e=>{const q=e.target.value.toLowerCase(),results=document.getElementById('results');results.replaceChildren();let count=0;for(const o of data.objects){if(q&&(o.name+' '+o.class_name+' '+o.source+' '+JSON.stringify(o.class_name==='MonoScript'?o.data:{})).toLowerCase().includes(q)){count++;if(count<=100)link(o.id,results)}}element('p',count+' objects; showing first 100',results)};
const initial=location.hash.slice(1),initialScope=report.scopes.find(s=>initial==='scope/'+s.id);
if(initialScope)scope(initialScope);else if(initial.startsWith('object/')&&objects.has(initial.slice(7)))object(initial.slice(7));else overview();
</script></html>"""
