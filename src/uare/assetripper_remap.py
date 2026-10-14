"""Map an AssetRipper export back to source objects and restore original asset GUIDs.

AssetRipper 2.0 gives every exported asset a random GUID (`UnityGuid.NewGuid()` in
AssetExportCollection) and numbers objects inside a file by insertion order, so nothing in its output
points back to a source object. Each exported asset is therefore matched to a game-model object by
content, per type, and only a one-to-one match is accepted:

* Texture2D: hash of the decoded RGBA pixels and size, against the package's own PNG of the object;
* Font: hash of the font file bytes, against the package's exported font;
* Material: name plus every saved float and color property;
* AnimationClip: name, sample rate, stop time and event function names;
* AnimatorController: name, with its parameter, layer and state names inside the original string table;
* AnimatorOverrideController: name and number of clip overrides;
* prefab: the full hierarchy (GameObject names, component classes in order, child order). The aligned
  walk also pairs each AssetRipper file ID with the source path ID.

A matched asset whose source object lives in a serialized file named by its source asset GUID (layout
verified by package GUID anchors) gets that GUID back; the export is copied with every reference
rewritten. Anything ambiguous, unmatched or claimed twice keeps its AssetRipper GUID and is reported.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from uare.blueprint import classify_scopes, guid_named_layout
from uare.errors import UareError
from uare.game_model import GameModel, Json, ObjectRecord, Reference, load_model, non_finite, outgoing
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.assetripper_remap")

_GUID_FILE = re.compile(r"[0-9a-f]{32}")
_GUID_REF = re.compile(r"\bguid: ([0-9a-f]{32})\b")
_GUID_REF_BYTES = re.compile(rb"\bguid: ([0-9a-f]{32})\b")
_DOC = re.compile(r"^--- !u!(\d+) &(-?\d+)[^\n]*$", re.M)
_FILE_REF = re.compile(r"\{fileID: (-?\d+)")
# Unity class IDs of the documents read here (UnityEngine YAML serialization).
_CLASS_NAMES = {1: "GameObject", 4: "Transform", 224: "RectTransform"}


class RemapError(UareError):
    """The AssetRipper export or the game model cannot be remapped."""


@dataclass(frozen=True)
class Match:
    path: str
    kind: str
    assetripper_guid: str
    status: str  # matched | ambiguous | unmatched | conflict
    object_id: str | None
    original_guid: str | None
    evidence: str
    file_ids: dict[str, int]


@dataclass(frozen=True)
class RemapResult:
    assets: int
    by_status: dict[str, int]
    by_kind: dict[str, dict[str, int]]
    guids_restored: int
    guid_layout_verified: bool
    out_dir: str


# -- AssetRipper side ------------------------------------------------------------------------------

def documents(text: str) -> list[tuple[int, int, str]]:
    """(class ID, file ID, body) of every YAML document of a Unity asset file."""
    heads = list(_DOC.finditer(text))
    return [(int(h[1]), int(h[2]), text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)])
            for i, h in enumerate(heads)]


def scalar(text: str) -> str:
    """A YAML plain, single-quoted ('' escapes a quote) or double-quoted (JSON-style escapes) scalar."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            return str(json.loads(text))
        except ValueError as exc:
            raise RemapError(f"Unreadable double-quoted YAML scalar; value={text[:80]}") from exc
    return text


def _field(body: str, key: str, indent: int = 2) -> str | None:
    match = re.search(rf"^{' ' * indent}{re.escape(key)}: ?(.*)$", body, re.M)
    return scalar(match[1]) if match else None


def _block(body: str, key: str, indent: int) -> list[str]:
    """Lines of a nested block under `key` at `indent`, until the next key at that indent or less."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line == " " * indent + key + ":":
            block = []
            for item in lines[index + 1:]:
                depth = len(item) - len(item.lstrip(" "))
                if item.strip() and (depth < indent or (depth == indent and not item.lstrip().startswith("- "))):
                    break
                block.append(item)
            return block
    return []


def _number(text: str) -> float:
    return float(text.replace("Infinity", "inf"))


def _rounded(value: float) -> str:
    return "nan" if value != value else ("inf" if value == float("inf") else ("-inf" if value == float("-inf")
                                                                               else f"{value:.5g}"))


def prefab_tree(text: str) -> tuple[str, list[int]] | None:
    """Hierarchy fingerprint of a prefab file and its file IDs in walk order (GameObject, then components)."""
    docs = documents(text)
    classes = {fid: cls for cls, fid, _ in docs}
    names: dict[int, str] = {}
    components: dict[int, list[int]] = {}
    owner: dict[int, int] = {}
    father: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    for cls, fid, body in docs:
        if cls == 1:
            names[fid] = _field(body, "m_Name") or ""
            components[fid] = [int(m[1]) for m in re.finditer(r"^  - component: \{fileID: (-?\d+)\}", body, re.M)]
        elif cls in (4, 224):
            owner[fid] = int(_FILE_REF.search(_field(body, "m_GameObject") or "{fileID: 0")[1])
            father[fid] = int(_FILE_REF.search(_field(body, "m_Father") or "{fileID: 0")[1])
            children[fid] = [int(m[1]) for m in _FILE_REF.finditer("\n".join(_block(body, "m_Children", 2)))]
    roots = [t for t, f in father.items() if f == 0]
    if len(roots) != 1:
        return None
    order: list[int] = []

    def walk(transform: int) -> str:
        go = owner[transform]
        order.append(go)
        order.extend(components.get(go, []))
        kinds = ",".join(_class_name(classes.get(c, 0)) for c in components.get(go, []))
        return f"{names.get(go, '')}|{kinds}[" + ";".join(walk(c) for c in children.get(transform, [])) + "]"

    return walk(roots[0]), order


def _class_name(class_id: int) -> str:
    if class_id in _CLASS_NAMES:
        return _CLASS_NAMES[class_id]
    from UnityPy.enums import ClassIDType  # required by remap(); a missing UnityPy must not degrade matching
    try:
        return ClassIDType(class_id).name
    except ValueError:
        return f"class{class_id}"


def _pixels(path: Path) -> str:
    from PIL import Image
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        return hashlib.sha256(f"{rgba.size}".encode() + rgba.tobytes()).hexdigest()


def _size(path: Path) -> str:
    from PIL import Image
    with Image.open(path) as image:
        return f"{image.size[0]}x{image.size[1]}"


def near_pixels(left: Path, right: Path, tolerance: int = 2) -> bool:
    """Same size and every channel within `tolerance`; RGB is ignored where both pixels are transparent.

    Texture decoders differ by rounding (measured: at most 1 per channel between AssetRipper and UnityPy).
    """
    from PIL import Image
    with Image.open(left) as a, Image.open(right) as b:
        if a.size != b.size:
            return False
        x, y = a.convert("RGBA").tobytes(), b.convert("RGBA").tobytes()
    for offset in range(0, len(x), 4):
        if x[offset + 3] == 0 and y[offset + 3] == 0:
            continue
        if any(abs(x[offset + c] - y[offset + c]) > tolerance for c in range(4)):
            return False
    return True


def _bytes_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _material_print(name: str, floats: dict[str, str], colors: dict[str, str]) -> str:
    return json.dumps([name, sorted(floats.items()), sorted(colors.items())])


def assetripper_fingerprint(path: Path, kind: str) -> tuple[str, list[int]] | None:
    """Fingerprint of one exported asset, comparable with `model_fingerprints`; None when it has no main object."""
    if kind == "Texture2D":
        return _pixels(path), []
    if kind == "Font":
        return _bytes_hash(path), []
    text = path.read_text(encoding="utf-8", errors="replace")
    if kind == "prefab":
        return prefab_tree(text)
    docs = documents(text)
    if not docs:
        return None
    body = docs[0][2]
    name = _field(body, "m_Name") or ""
    if kind == "Material":
        # Unity writes saved properties as a list ("- _Name: v") or, since 2021, as a map ("_Name: v").
        item = r"^ {4,6}(?:- )?(\S+): (.+)$"
        floats = {m[1]: _rounded(_number(m[2])) for m in re.finditer(item, "\n".join(_block(body, "m_Floats", 4)), re.M)}
        colors = {m[1]: ",".join(_rounded(_number(v)) for v in re.findall(r"[rgba]: ([^,}]+)", m[2]))
                  for m in re.finditer(item, "\n".join(_block(body, "m_Colors", 4)), re.M) if m[2].startswith("{")}
        return _material_print(name, floats, colors), []
    if kind == "AnimationClip":
        events = [m[1] for m in re.finditer(r"functionName: (\S*)", "\n".join(_block(body, "m_Events", 2)))]
        sample = _rounded(_number(_field(body, "m_SampleRate") or "0"))
        stop = _rounded(_number(_field(body, "m_StopTime", 4) or "0"))
        return json.dumps([name, sample, stop, events]), []
    if kind == "AnimatorController":
        names = {_field(b, "m_Name") or "" for c, _, b in docs[1:] if c in (1102, 1107)}
        names |= {scalar(m[1]) for m in re.finditer(r"^  - m_Name: (.+)$", "\n".join(_block(body, "m_AnimatorParameters", 2)), re.M)}
        # Inside m_AnimatorLayers only layer names sit at indent 4.
        names |= {scalar(m[1]) for m in re.finditer(r"^    m_Name: (.+)$", "\n".join(_block(body, "m_AnimatorLayers", 2)), re.M)}
        return json.dumps([name, sorted(n for n in names if n)]), []
    if kind == "AnimatorOverrideController":
        return json.dumps([name, len(re.findall(r"m_OriginalClip:", body))]), []
    raise RemapError(f"No fingerprint for AssetRipper asset kind {kind}")


def exported_assets(project: Path) -> Iterator[tuple[Path, str, str]]:
    """(path, kind, AssetRipper GUID) of every exported asset this module can match."""
    assets = project / "Assets"
    suffixes = {".anim": "AnimationClip", ".controller": "AnimatorController", ".mat": "Material",
                ".overrideController": "AnimatorOverrideController", ".prefab": "prefab",
                ".ttf": "Font", ".otf": "Font"}
    for path in sorted(assets.rglob("*")):
        meta = path.with_name(path.name + ".meta")
        if not path.is_file() or not meta.is_file():
            continue
        guid_match = _GUID_REF.search(meta.read_text(encoding="utf-8", errors="replace"))
        if guid_match is None:
            continue
        kind = suffixes.get(path.suffix)
        if kind is None and path.suffix.lower() == ".png" and "TextureImporter:" in meta.read_text(errors="replace"):
            kind = "Texture2D"
        if kind is not None:
            yield path, kind, guid_match[1]


# -- source side -----------------------------------------------------------------------------------

def _tagged_number(value: Json) -> str:
    spelling = non_finite(value)
    if spelling is not None:
        return {"NaN": "nan", "Infinity": "inf", "-Infinity": "-inf"}[spelling]
    return _rounded(float(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def _target(edges: dict[str, tuple[Reference, ...]], oid: str, field: str) -> str | None:
    return next((r.target_id for r in edges.get(oid, ()) if r.field == field and r.status == "resolved"), None)


def _indexed(edges: dict[str, tuple[Reference, ...]], oid: str, prefix: str, suffix: str = "") -> list[str]:
    found = [(int(r.field[len(prefix):].split("]", 1)[0]), r.target_id) for r in edges.get(oid, ())
             if r.field.startswith(prefix) and r.field.endswith(suffix) and r.target_id and r.status == "resolved"]
    return [target for _, target in sorted(found)]


def _source_prefab(root: str, by_id: dict[str, ObjectRecord], edges: dict[str, tuple[Reference, ...]]) -> tuple[str, list[str]]:
    order: list[str] = []

    def walk(go: str) -> str:
        comps = _indexed(edges, go, "m_Component[", "].component")
        order.append(go)
        order.extend(comps)
        transform = next((c for c in comps if by_id[c].class_name in ("Transform", "RectTransform")), None)
        kids = [_target(edges, t, "m_GameObject") for t in _indexed(edges, transform, "m_Children[")] if transform else []
        name = str(by_id[go].data.get("m_Name", by_id[go].name))
        return f"{name}|{','.join(by_id[c].class_name for c in comps)}[" + ";".join(walk(k) for k in kids if k) + "]"

    return walk(root), order


def model_fingerprints(model: GameModel, assets_dir: Path, kinds: set[str]) -> dict[str, dict[str, list[tuple[str, list[str]]]]]:
    """kind -> fingerprint -> [(object id, path ids in walk order)] for every source object of those kinds."""
    by_id = {o.id: o for o in model.objects}
    edges = outgoing(model)
    prints: dict[str, dict[str, list[tuple[str, list[str]]]]] = defaultdict(lambda: defaultdict(list))
    for obj in model.objects:
        kind, data = obj.class_name, obj.data
        if kind not in kinds:
            continue
        if kind in ("Texture2D", "Font"):
            if not obj.output or not (assets_dir / obj.output).is_file():
                continue
            if kind == "Texture2D" and not obj.output.endswith(".png"):
                continue
            if kind == "Texture2D":
                prints["Texture2D-size"][_size(assets_dir / obj.output)].append((obj.id, []))
            key = _pixels(assets_dir / obj.output) if kind == "Texture2D" else _bytes_hash(assets_dir / obj.output)
        elif kind == "Material":
            props = data.get("m_SavedProperties") if isinstance(data.get("m_SavedProperties"), dict) else {}
            floats = {str(p[0]): _tagged_number(p[1]) for p in props.get("m_Floats") or [] if isinstance(p, list) and len(p) == 2}
            colors = {str(p[0]): ",".join(_tagged_number(p[1].get(c)) for c in "rgba")
                      for p in props.get("m_Colors") or [] if isinstance(p, list) and len(p) == 2 and isinstance(p[1], dict)}
            key = _material_print(obj.name, floats, colors)
        elif kind == "AnimationClip":
            muscle = data.get("m_MuscleClip") if isinstance(data.get("m_MuscleClip"), dict) else {}
            events = [str(e.get("functionName", "")) for e in data.get("m_Events") or [] if isinstance(e, dict)]
            key = json.dumps([obj.name, _tagged_number(data.get("m_SampleRate")), _tagged_number(muscle.get("m_StopTime")), events])
        elif kind == "AnimatorController":
            # The exported names must all be in the original string table; the match is checked per candidate.
            key = obj.name
        elif kind == "AnimatorOverrideController":
            key = json.dumps([obj.name, len(data.get("m_Clips") or [])])
        else:
            raise RemapError(f"No source fingerprint for kind {kind}")
        prints[kind][key].append((obj.id, []))
    if "prefab" in kinds:
        # Every root GameObject outside scene files: classified prefabs and the unclassified hierarchies of
        # shared files alike. Only a match in a GUID-named file restores a GUID.
        scenes = {(s.source, s.serialized_file) for s in classify_scopes(model, by_id, edges) if s.kind == "scene"}
        for obj in model.objects:
            if obj.class_name != "GameObject" or (obj.source, obj.serialized_file) in scenes:
                continue
            transform = next((c for c in _indexed(edges, obj.id, "m_Component[", "].component")
                              if by_id[c].class_name in ("Transform", "RectTransform")), None)
            if transform is not None and _target(edges, transform, "m_Father") is None:
                key, order = _source_prefab(obj.id, by_id, edges)
                prints["prefab"][key].append((obj.id, order))
    return prints


def _controller_names(obj: ObjectRecord) -> set[str]:
    table = obj.data.get("m_TOS")
    return {str(entry[1]) for entry in table if isinstance(entry, list) and len(entry) == 2} if isinstance(table, list) else set()


# -- matching and rewriting ------------------------------------------------------------------------

def match_export(project: Path, model: GameModel, assets_dir: Path,
                 fingerprint: Callable[[Path, str], tuple[str, list[int]] | None] = assetripper_fingerprint) -> list[Match]:
    """One Match per exported asset; only one-to-one content matches are `matched`."""
    exported = list(exported_assets(project))
    kinds = {kind for _, kind, _ in exported}
    prints = model_fingerprints(model, assets_dir, kinds)
    by_id = {o.id: o for o in model.objects}
    layout = guid_named_layout(model)
    pending: list[tuple[Path, str, str, list[tuple[str, list[str]]], list[int], str]] = []
    for path, kind, guid in exported:
        result = fingerprint(path, kind)
        if result is None:
            pending.append((path, kind, guid, [], [], "no main object"))
            continue
        key, order = result
        if kind == "AnimatorController":
            name, names = json.loads(key)
            candidates = [c for c in prints[kind].get(name, []) if set(names) <= _controller_names(by_id[c[0]])]
            evidence = "name; parameter, layer and state names in the original string table"
        else:
            candidates = prints[kind].get(key, [])
            evidence = {"Texture2D": "decoded pixel hash", "Font": "font file hash", "prefab": "hierarchy fingerprint",
                        "Material": "name and saved float/color properties", "AnimationClip": "name, sample rate, stop time, events",
                        "AnimatorOverrideController": "name and clip override count"}[kind]
        if kind == "Texture2D" and not candidates:
            near = [c for c in prints["Texture2D-size"].get(_size(path), [])
                    if near_pixels(path, assets_dir / str(by_id[c[0]].output))]
            if near:
                candidates, evidence = near, "decoded pixels within 2 per channel (decoder rounding), transparent RGB ignored"
        if len(candidates) > 1:
            # Identical content in several source objects: only a single same-named candidate is accepted.
            stem = path.stem
            for name in (stem, re.sub(r"[ _]\d+$", "", stem)):
                named = [c for c in candidates if by_id[c[0]].name == name]
                if len(named) == 1:
                    candidates, evidence = named, evidence + "; unique same-named candidate among identical content"
                if named:
                    break  # an exact-name result is final; the suffix-stripped name never overrides it
        pending.append((path, kind, guid, candidates, order, evidence))
    claims = Counter(c[0][0] for _, _, _, c, _, _ in pending if len(c) == 1)
    matched: list[tuple[Path, str, str, str, list[str], list[int], str]] = []
    results: list[Match] = []
    for path, kind, guid, candidates, order, evidence in pending:
        rel = path.relative_to(project).as_posix()
        if len(candidates) != 1:
            status = "unmatched" if not candidates else "ambiguous"
            results.append(Match(rel, kind, guid, status, None, None, f"{evidence}; {len(candidates)} candidates", {}))
        elif claims[candidates[0][0]] > 1:
            results.append(Match(rel, kind, guid, "conflict", candidates[0][0], None, f"{evidence}; claimed twice", {}))
        else:
            matched.append((path, kind, guid, candidates[0][0], candidates[0][1], order, evidence))
    per_file = Counter(by_id[oid].serialized_file for _, _, _, oid, _, _, _ in matched)
    file_objects: dict[tuple[str, str], list[ObjectRecord]] = defaultdict(list)
    for item in model.objects:
        file_objects[item.source, item.serialized_file].append(item)
    for path, kind, guid, oid, source_order, order, evidence in matched:
        obj = by_id[oid]
        sole = _sole_asset(obj, kind, set(source_order), file_objects[obj.source, obj.serialized_file])
        original = (obj.serialized_file if layout and _GUID_FILE.fullmatch(obj.serialized_file)
                    and per_file[obj.serialized_file] == 1 and sole else None)
        file_ids = ({str(fid): by_id[sid].path_id for fid, sid in zip(order, source_order)}
                    if kind == "prefab" and len(order) == len(source_order) else {})
        results.append(Match(path.relative_to(project).as_posix(), kind, guid, "matched", oid, original, evidence, file_ids))
    return results


# Objects a source asset file may hold besides its main object: a texture's sprites, a font's atlas and material.
_SUB_OBJECTS = {"Texture2D": {"Sprite"}, "Font": {"Texture2D", "Material"}}


def _sole_asset(obj: ObjectRecord, kind: str, structure: set[str], siblings: list[ObjectRecord]) -> bool:
    """True when `obj` is the main object of its file: every other object is one of its own sub-objects.

    A file whose main asset is something else (a TMP font asset holding a Material) must not lend its GUID.
    """
    if kind == "prefab":
        return all(s.id in structure for s in siblings)
    allowed = _SUB_OBJECTS.get(kind, set())
    return all(s.id == obj.id or s.class_name in allowed for s in siblings)


def rewrite_project(project: Path, target: Path, guids: dict[str, str]) -> None:
    """Copy `project` to `target`, replacing AssetRipper GUIDs by original ones in every non-binary file.

    Files are copied, never linked, so editing the copy cannot change the AssetRipper export. Text is
    rewritten as bytes: no size limit, line endings and encoding stay as they are.
    """
    taken = {g.encode() for g in guids.values()}
    mapping = {old.encode(): new.encode() for old, new in guids.items()}
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if path.is_symlink():
            raise RemapError(f"AssetRipper export contains a symlink; refusing to follow it; path={rel}")
        out = target / rel
        if path.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        if b"\0" in data[:8192] or b"guid: " not in data:
            shutil.copy2(path, out)
            continue
        if path.suffix == ".meta":
            own = _GUID_REF_BYTES.search(data)
            if own and own[1] in taken and own[1] not in mapping:
                raise RemapError(f"Original GUID already used by another exported asset; path={rel}")
        out.write_bytes(_GUID_REF_BYTES.sub(lambda m: b"guid: " + mapping.get(m[1], m[1]), data))
        shutil.copystat(path, out)


def remap(export_dir: Path, assets_dir: Path, out_dir: Path) -> RemapResult:
    """Match `export_dir` (from `uare assetripper`) to the package model and write a GUID-restored copy."""
    project = export_dir / "export" / "ExportedProject"
    if not (project / "Assets").is_dir():
        raise RemapError(f"Not an AssetRipper export from `uare assetripper`; missing {project / 'Assets'}")
    for module in ("UnityPy", "PIL"):
        # UnityPy names component classes and Pillow decodes pixels; without them every match would fail silently.
        if importlib.util.find_spec(module) is None:
            raise RemapError(f"assetripper-remap needs {module}; install the project's `power` extra")
    model = load_model(assets_dir / "game_model.json")
    matches = match_export(project, model, assets_dir)
    guids = {m.assetripper_guid: m.original_guid for m in matches if m.original_guid}
    with staged_output(out_dir) as staging:
        rewrite_project(project, staging / "project", guids)
        (staging / "mapping.json").write_text(json.dumps([asdict(m) for m in matches], indent=1) + "\n", encoding="utf-8")
        by_kind: dict[str, Counter[str]] = defaultdict(Counter)
        for m in matches:
            by_kind[m.kind][m.status] += 1
        result = RemapResult(len(matches), dict(Counter(m.status for m in matches)),
                             {k: dict(v) for k, v in sorted(by_kind.items())}, len(guids), guid_named_layout(model), str(out_dir))
        (staging / "report.json").write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    event(LOG, logging.INFO, "assetripper_remapped", assets=result.assets, restored=result.guids_restored)
    return result


def summarize(result: RemapResult) -> list[str]:
    return [f"exported assets   : {result.assets} ({', '.join(f'{k}={v}' for k, v in sorted(result.by_status.items()))})",
            *(f"  {kind:26}: {', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}" for kind, counts in result.by_kind.items()),
            f"guid layout       : {'verified' if result.guid_layout_verified else 'not verified (no GUIDs restored)'}",
            f"original GUIDs    : {result.guids_restored} restored",
            f"output            : {result.out_dir}/project (remapped copy), mapping.json, report.json"]
