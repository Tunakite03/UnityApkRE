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
  walk also pairs each AssetRipper file ID with the source path ID;
* TextAsset: hash of the file bytes against the package's exported text, else a name unique among text assets;
* ScriptableObject (a MonoBehaviour asset): name plus its serialized field names;
* Mesh and AudioClip: a name unique among objects of that class (weakest evidence, reported as such).

A matched asset whose source object lives in a serialized file named by its source asset GUID (layout
verified by package GUID anchors) gets that GUID back; the export is copied with every reference
rewritten. Anything ambiguous, unmatched or claimed twice keeps its AssetRipper GUID and is reported.

With an Addressables catalog, a matched asset whose source object is the target of an AssetBundle container
entry keyed by an asset GUID gets that GUID and the catalog's original path: the file and its .meta move to
that path, keeping the exported extension (an original .fbx exported as a prefab stays a .prefab in the
same folder). A path or GUID claimed by two exported assets is not applied to either and is reported.

A build can hold several copies of one source asset (a player scene's `sharedassets` and an Addressables bundle).
Their exports have identical content and therefore the same candidate set. When exactly one candidate of such a set
has a catalog identity, the copies are merged: one export takes the original GUID and path, the others are dropped
and every reference to them is rewritten to that GUID (kinds whose main object has a fixed file ID only).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from uare.addressables import asset_paths as catalog_asset_paths
from uare.atlas_split import AtlasSplitResult, split_atlas_sprites
from uare.atlas_split import summarize as summarize_atlas
from uare.blueprint import classify_scopes, container_keys, guid_named_layout
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
    status: str  # matched | ambiguous | unmatched | conflict | duplicate
    object_id: str | None
    original_guid: str | None
    evidence: str
    file_ids: dict[str, int]
    # Project path the asset moves to (original folder and name, exported extension), from an Addressables catalog.
    original_path: str | None = None
    # Source objects with identical content (ambiguous matches), and the export a duplicate was merged into.
    candidates: tuple[str, ...] = ()
    merged_into: str | None = None


@dataclass(frozen=True)
class RemapResult:
    assets: int
    by_status: dict[str, int]
    by_kind: dict[str, dict[str, int]]
    guids_restored: int
    guid_layout_verified: bool
    out_dir: str
    paths_restored: int = 0
    catalog_conflicts: int = 0
    duplicates_merged: int = 0
    scenes_restored: int = 0
    guid_named_restored: int = 0
    atlas_split: AtlasSplitResult | None = None


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
    from PIL import Image, ImageChops
    with Image.open(left) as a, Image.open(right) as b:
        if a.size != b.size:
            return False
        x, y = a.convert("RGBA"), b.convert("RGBA")
    # Pixels transparent in both images are cleared in both, then the per-channel difference is checked in C.
    both_clear = ImageChops.lighter(x.getchannel("A"), y.getchannel("A")).point(lambda v: 255 if v == 0 else 0)
    blank = Image.new("RGBA", x.size, (0, 0, 0, 0))
    x, y = Image.composite(blank, x, both_clear), Image.composite(blank, y, both_clear)
    return all(high <= tolerance for _, high in ImageChops.difference(x, y).getextrema())


def _bytes_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _material_print(name: str, floats: dict[str, str], colors: dict[str, str]) -> str:
    return json.dumps([name, sorted(floats.items()), sorted(colors.items())])


def assetripper_fingerprint(path: Path, kind: str) -> tuple[str, list[int]] | None:
    """Fingerprint of one exported asset, comparable with `model_fingerprints`; None when it has no main object."""
    if kind == "Texture2D":
        return _pixels(path), []
    if kind in ("Font", "TextAsset"):
        return _bytes_hash(path), []
    if kind == "AudioClip":
        return json.dumps([path.stem]), []
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
    if kind == "ScriptableObject":
        return json.dumps([name, _field_names(body)]), []
    if kind == "Mesh":
        return json.dumps([name]), []
    raise RemapError(f"No fingerprint for AssetRipper asset kind {kind}")


def _field_names(body: str) -> list[str]:
    """Serialized field names of a MonoBehaviour document, without Unity's own `m_` fields."""
    return sorted({m[1] for m in re.finditer(r"^  ([A-Za-z_][\w]*):", body, re.M) if not m[1].startswith("m_")})


def _first_class(path: Path) -> int | None:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        head = handle.read(4096)
    match = _DOC.search(head)
    return int(match[1]) if match else None


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
        suffix = path.suffix.lower()
        if kind is None and suffix in (".png", ".bytes", ".txt", ".json", ".xml", ".csv", ".ogg", ".wav", ".mp3"):
            importer = meta.read_text(errors="replace")
            kind = ("Texture2D" if suffix == ".png" and "TextureImporter:" in importer else
                    "TextAsset" if "TextScriptImporter:" in importer else
                    "AudioClip" if "AudioImporter:" in importer else None)
        elif kind is None and suffix == ".asset":
            cls = _first_class(path)
            kind = "Mesh" if cls == 43 else "ScriptableObject" if cls == 114 else None
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
        if kind == "MonoBehaviour" and "ScriptableObject" in kinds:
            kind = "ScriptableObject"
        if kind not in kinds:
            continue
        if kind == "ScriptableObject":
            ref = next((r for r in edges.get(obj.id, ()) if r.field == "m_GameObject"), None)
            if obj.class_name != "MonoBehaviour" or (ref is not None and ref.status != "null"):
                continue
            prints["ScriptableObject"][json.dumps([obj.name, sorted(k for k in data if not k.startswith("m_"))])].append((obj.id, []))
            continue
        if kind in ("Mesh", "AudioClip"):
            prints[kind][json.dumps([obj.name])].append((obj.id, []))
            continue
        if kind == "TextAsset":
            if obj.output and (assets_dir / obj.output).is_file():
                prints[kind][_bytes_hash(assets_dir / obj.output)].append((obj.id, []))
            prints["TextAsset-name"][obj.name].append((obj.id, []))
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
                        "AnimatorOverrideController": "name and clip override count", "TextAsset": "file byte hash",
                        "ScriptableObject": "name and serialized field names", "Mesh": "unique name only",
                        "AudioClip": "unique name only"}[kind]
        if kind == "TextAsset" and not candidates:
            candidates, evidence = prints["TextAsset-name"].get(path.stem, []), "unique name only (bytes differ)"
        if kind == "Texture2D" and not candidates:
            # Same-named textures of that size are compared first; the whole size class only when none matches,
            # because decoding every same-size texture for each export is quadratic on large builds.
            same_size = prints["Texture2D-size"].get(_size(path), [])
            names = {path.stem, re.sub(r"[ _]\d+$", "", path.stem)}
            named = [c for c in same_size if by_id[c[0]].name in names]
            near = [c for c in named if near_pixels(path, assets_dir / str(by_id[c[0]].output))]
            scope = "same-named textures"
            if not near:
                near = [c for c in same_size if c not in named and near_pixels(path, assets_dir / str(by_id[c[0]].output))]
                scope = "all textures of that size"
            if near:
                candidates = near
                evidence = f"decoded pixels within 2 per channel (decoder rounding), transparent RGB ignored; among {scope}"
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
            results.append(Match(rel, kind, guid, status, None, None, f"{evidence}; {len(candidates)} candidates", {},
                                 candidates=tuple(sorted(c[0] for c in candidates))))
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


def catalog_identities(model: GameModel, paths: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """Object id -> (asset GUID, original path) for container targets keyed by a GUID the catalog names.

    An object named by several catalog GUIDs is left out.
    """
    found: dict[str, set[str]] = defaultdict(set)
    for oid, keys in container_keys(model, outgoing(model)).items():
        found[oid].update(key for key in keys if key in paths)
    return {oid: (guid, paths[guid]) for oid, guids in found.items() if len(guids) == 1 for guid in guids}


def apply_catalog(matches: list[Match], identities: Mapping[str, tuple[str, str]],
                  project_files: set[str]) -> tuple[list[Match], int]:
    """Give matched assets their catalog GUID and original path; returns the updated matches and the conflict count.

    The target keeps the exported extension. Several exports of one catalog asset are the sub-assets of one source
    file (a model's prefab, meshes and clips): the single prefab among them takes the GUID and path, the others move
    into a folder named after the source file next to it and keep their own GUIDs. A path proposed twice, a target
    that is another exported file staying in place, or a GUID that disagrees with one restored by layout is applied
    to none of them.
    """
    proposals: dict[int, tuple[str, str]] = {}
    for index, match in enumerate(matches):
        if match.status != "matched" or match.object_id not in identities:
            continue
        guid, original = identities[match.object_id]
        if not original.startswith("Assets/") or ".." in original or (match.original_guid and match.original_guid != guid):
            continue
        proposals[index] = (guid, original)
    by_guid: dict[str, list[int]] = defaultdict(list)
    for index, (guid, _) in proposals.items():
        by_guid[guid].append(index)
    targets: dict[int, tuple[str | None, str, str]] = {}  # GUID or None, target path, evidence
    for guid, indexes in by_guid.items():
        original = PurePosixPath(proposals[indexes[0]][1])
        if len(indexes) == 1:
            index = indexes[0]
            targets[index] = (guid, str(original.with_suffix(PurePosixPath(matches[index].path).suffix)),
                              "GUID and path from Addressables catalog")
            continue
        mains = [i for i in indexes if matches[i].kind == "prefab"]
        main = mains[0] if len(mains) == 1 else None
        folder = original.with_suffix("")
        for index in indexes:
            if index == main:
                targets[index] = (guid, str(original.with_suffix(".prefab")),
                                  f"GUID and path from Addressables catalog; main object of {len(indexes)} sub-assets")
            else:
                targets[index] = (None, f"{folder}/{PurePosixPath(matches[index].path).name}",
                                  f"sub-asset of catalog asset {original.name}; folder from the catalog path, own GUID kept")
    moved_from = {matches[i].path.casefold() for i in targets}
    staying = {path.casefold() for path in project_files} - moved_from
    path_count = Counter(target.casefold() for _, target, _ in targets.values())
    updated = list(matches)
    conflicts = 0
    for index, (guid, target, evidence) in targets.items():
        match = matches[index]
        if path_count[target.casefold()] > 1 or target.casefold() in staying:
            conflicts += 1
            updated[index] = replace(match, evidence=match.evidence + "; catalog path claimed twice, not applied")
            continue
        updated[index] = replace(match, original_guid=guid or match.original_guid, original_path=target,
                                 evidence=f"{match.evidence}; {evidence}")
    return updated, conflicts


def catalog_scene_moves(project: Path, paths: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Moves and GUIDs for exports AssetRipper names by their asset GUID (`<guid>.unity`, `<guid>.shader`, ...).

    When a bundle's container key is a GUID and AssetRipper cannot name the asset otherwise (streamed scenes, shaders),
    the file name is that GUID. Returns (project-relative path -> original path with the exported extension,
    AssetRipper GUID -> asset GUID) for every such file the catalog names; a scene's companion folder (lighting data)
    moves next to it.
    """
    moves: dict[str, str] = {}
    guids: dict[str, str] = {}
    assets = project / "Assets"
    for item in sorted(assets.rglob("*")):
        guid = item.stem
        if not item.is_file() or item.suffix == ".meta" or not _GUID_FILE.fullmatch(guid):
            continue
        original = paths.get(guid, "")
        if not original.startswith("Assets/") or ".." in original or (item.suffix == ".unity") != original.endswith(".unity"):
            continue
        moves[item.relative_to(project).as_posix()] = str(PurePosixPath(original).with_suffix(item.suffix))
        meta = item.with_name(item.name + ".meta")
        own = _GUID_REF.search(meta.read_text(encoding="utf-8", errors="replace")) if meta.is_file() else None
        if own:
            guids[own[1]] = guid
        companion = item.with_name(guid)
        if item.suffix == ".unity" and companion.is_dir():
            target = str(PurePosixPath(original).with_suffix(""))
            moves[companion.relative_to(project).as_posix()] = target
            for part in companion.rglob("*"):
                if part.is_file() and part.suffix != ".meta":
                    moves[part.relative_to(project).as_posix()] = target + "/" + part.relative_to(companion).as_posix()
    return moves, guids


# AssetRipper's per-class folders and file suffixes, for exports that no evidence places at an original path.
UNMAPPED_ROOT = "Assets/_Unmapped/AssetRipper"
_KEEP_ROOTS = frozenset({"Plugins", "Scripts", "Resources", "Scene"})
_SUFFIX_FOLDERS = {".prefab": "Prefab", ".mat": "Material", ".anim": "AnimationClip", ".controller": "AnimatorController",
                   ".overrideController": "AnimatorController", ".png": "Texture2D", ".asset": "Asset", ".bytes": "TextAsset",
                   ".txt": "TextAsset", ".json": "TextAsset", ".shader": "Shader", ".ogg": "AudioClip", ".wav": "AudioClip",
                   ".unity": "Scene", ".mixer": "AudioMixerController", ".playable": "Playable"}


def unmapped_moves(project_files: set[str], moves: Mapping[str, str], original_roots: set[str]) -> dict[str, str]:
    """Moves for exports left where AssetRipper put them: files at the root of Assets/ and AssetRipper's type
    folders go under UNMAPPED_ROOT, so the project root shows only original folders. Code, Resources, player scenes
    and folders that hold catalog-placed assets stay."""
    targets: dict[str, str] = {}
    taken = {t.casefold() for t in moves.values()}
    for rel in sorted(project_files):
        if rel in moves or rel.endswith(".meta"):
            continue
        parts = rel.split("/")
        if len(parts) < 2 or parts[0] != "Assets":
            continue
        if len(parts) == 2:
            folder = _SUFFIX_FOLDERS.get(PurePosixPath(rel).suffix, "Other")
            target = f"{UNMAPPED_ROOT}/{folder}/{parts[1]}"
        elif parts[1] in _KEEP_ROOTS or parts[1] in original_roots or parts[1].startswith("_"):
            continue
        else:
            target = f"{UNMAPPED_ROOT}/{'/'.join(parts[1:])}"
            # The folder itself (its .meta) moves with its files.
            targets.setdefault(f"Assets/{parts[1]}", f"{UNMAPPED_ROOT}/{parts[1]}")
        stem, suffix, counter = target[:-len(PurePosixPath(target).suffix) or None], PurePosixPath(target).suffix, 1
        while target.casefold() in taken:
            target = f"{stem}_{counter}{suffix}"
            counter += 1
        taken.add(target.casefold())
        targets[rel] = target
    return targets


# Kinds whose exported main object always has the same file ID, so one GUID can stand for any copy.
_MERGEABLE = frozenset({"Texture2D", "Material", "Font", "TextAsset", "AudioClip", "AnimationClip", "AnimatorController"})


def merge_catalog_duplicates(matches: list[Match], identities: Mapping[str, tuple[str, str]]) -> list[Match]:
    """Resolve exports whose content is identical in several source objects.

    One catalog candidate: the first export (by path) becomes `matched` to it and the others become `duplicate` of it
    (mergeable kinds only). Several catalog candidates: exports and catalog candidates are paired in sorted order
    (export path, catalog path), since every pair has identical content; extra exports of a mergeable kind become
    duplicates of the first pair, others stay ambiguous. Candidate sets without a catalog identity stay ambiguous.
    Pairing places correct content at every catalog path, but a reference may point at an identical copy at another
    path; the evidence says so.
    """
    groups: dict[tuple[str, tuple[str, ...]], list[int]] = defaultdict(list)
    for index, match in enumerate(matches):
        if match.status == "ambiguous" and match.candidates:
            groups[match.kind, match.candidates].append(index)
    updated = list(matches)
    for (kind, candidates), indexes in groups.items():
        known = sorted((c for c in candidates if c in identities), key=lambda c: identities[c][1])
        if not known or (len(known) == 1 and kind not in _MERGEABLE):
            continue
        indexes.sort(key=lambda i: matches[i].path)
        if len(known) == 1:
            note = f"identical content in {len(candidates)} source objects; the catalog names one; {len(indexes)} exports merged"
        else:
            note = (f"identical content in {len(candidates)} source objects, {len(known)} named by the catalog; paired in "
                    f"sorted order, so a reference may resolve to an identical copy at another catalog path")
        pairs = list(zip(indexes, known))
        for index, candidate in pairs:
            updated[index] = replace(matches[index], status="matched", object_id=candidate,
                                     evidence=f"{matches[index].evidence}; {note}")
        if kind not in _MERGEABLE:
            continue
        first = matches[pairs[0][0]]
        for index in indexes[len(pairs):]:
            updated[index] = replace(matches[index], status="duplicate", object_id=pairs[0][1], merged_into=first.path,
                                     evidence=f"{matches[index].evidence}; {note}")
    return updated


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


def rewrite_project(project: Path, target: Path, guids: dict[str, str], moves: Mapping[str, str] | None = None,
                    skip: frozenset[str] = frozenset()) -> None:
    """Copy `project` to `target`, replacing AssetRipper GUIDs by original ones in every non-binary file.

    `moves` maps an asset's project-relative path to its new one; its .meta moves with it. Assets in `skip`
    (merged duplicates) are not copied, nor are their .meta files.

    Files are copied, never linked, so editing the copy cannot change the AssetRipper export. Text is
    rewritten as bytes: no size limit, line endings and encoding stay as they are.
    """
    taken = {g.encode() for g in guids.values()}
    mapping = {old.encode(): new.encode() for old, new in guids.items()}
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if path.is_symlink():
            raise RemapError(f"AssetRipper export contains a symlink; refusing to follow it; path={rel}")
        key = rel.as_posix()
        if (key[:-len(".meta")] if key.endswith(".meta") else key) in skip:
            continue
        moved = (moves or {}).get(key[:-len(".meta")] if key.endswith(".meta") else key)
        out = target / (moved + ".meta" if moved and key.endswith(".meta") else moved) if moved else target / rel
        if path.is_dir():
            if not moved:  # a moved folder's files recreate it at the target; the old path must not stay behind
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


def remap(export_dir: Path, assets_dir: Path, out_dir: Path, addressables_catalog: Path | None = None,
          split_atlases: bool = False) -> RemapResult:
    """Match `export_dir` (from `uare assetripper`) to the package model and write a GUID-restored copy.

    With `addressables_catalog`, matched assets and GUID-named files (scenes, shaders) also take their original GUID and project path
    from the catalog. With `split_atlases`, SpriteAtlas pages of the copy become one PNG per sprite.
    """
    project = export_dir / "export" / "ExportedProject"
    if not (project / "Assets").is_dir():
        raise RemapError(f"Not an AssetRipper export from `uare assetripper`; missing {project / 'Assets'}")
    for module in ("UnityPy", "PIL"):
        # UnityPy names component classes and Pillow decodes pixels; without them every match would fail silently.
        if importlib.util.find_spec(module) is None:
            raise RemapError(f"assetripper-remap needs {module}; install the project's `power` extra")
    catalog = catalog_asset_paths(addressables_catalog) if addressables_catalog else None
    model = load_model(assets_dir / "game_model.json")
    matches = match_export(project, model, assets_dir)
    conflicts = 0
    if catalog:
        files = {p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file()}
        identities = catalog_identities(model, catalog.paths)
        matches, conflicts = apply_catalog(merge_catalog_duplicates(matches, identities), identities, files)
        # A duplicate follows its merged export: same GUID when that export took one, otherwise it stays as it is.
        primary = {m.path: m for m in matches}
        matches = [replace(m, original_guid=primary[m.merged_into].original_guid)
                   if m.status == "duplicate" and m.merged_into and primary[m.merged_into].original_guid
                   else (replace(m, status="ambiguous", merged_into=None) if m.status == "duplicate" else m)
                   for m in matches]
    guids = {m.assetripper_guid: m.original_guid for m in matches if m.original_guid}
    skip = frozenset(m.path for m in matches if m.status == "duplicate")
    moves = {m.path: m.original_path for m in matches if m.original_path}
    scene_moves: dict[str, str] = {}
    if catalog:
        scene_moves, scene_guids = catalog_scene_moves(project, catalog.paths)
        taken = {target.casefold() for target in moves.values()}
        scene_moves = {k: v for k, v in scene_moves.items() if k not in moves and v.casefold() not in taken}
        scene_guids = {k: v for k, v in scene_guids.items() if k not in guids and v not in guids.values()}
        clashes = set(scene_guids.values()) & set(guids.values())
        if clashes:
            raise RemapError(f"A scene GUID from the catalog is also restored for another asset; guids={sorted(clashes)[:3]}")
        guids.update(scene_guids)
        moves.update(scene_moves)
        roots = {p.split("/")[1] for p in catalog.paths.values() if p.count("/") >= 2}
        files_now = {p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file()}
        moves.update(unmapped_moves(files_now - skip, moves, roots))
    with staged_output(out_dir) as staging:
        rewrite_project(project, staging / "project", guids, moves, skip)
        split = split_atlas_sprites(staging / "project", catalog.paths if catalog else None) if split_atlases else None
        (staging / "mapping.json").write_text(json.dumps([asdict(m) for m in matches], indent=1) + "\n", encoding="utf-8")
        by_kind: dict[str, Counter[str]] = defaultdict(Counter)
        for m in matches:
            by_kind[m.kind][m.status] += 1
        result = RemapResult(len(matches), dict(Counter(m.status for m in matches)),
                             {k: dict(v) for k, v in sorted(by_kind.items())}, len(guids), guid_named_layout(model), str(out_dir),
                             paths_restored=sum(1 for m in matches if m.original_path), catalog_conflicts=conflicts,
                             duplicates_merged=len(skip),
                             scenes_restored=sum(target.endswith(".unity") for target in scene_moves.values()),
                             guid_named_restored=sum(1 for source, target in scene_moves.items()
                                                     if not target.endswith(".unity")
                                                     and _GUID_FILE.fullmatch(PurePosixPath(source).stem)),
                             atlas_split=split)
        (staging / "report.json").write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    event(LOG, logging.INFO, "assetripper_remapped", assets=result.assets, restored=result.guids_restored)
    return result


def summarize(result: RemapResult) -> list[str]:
    return [f"exported assets   : {result.assets} ({', '.join(f'{k}={v}' for k, v in sorted(result.by_status.items()))})",
            *(f"  {kind:26}: {', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}" for kind, counts in result.by_kind.items()),
            f"guid layout       : {'verified' if result.guid_layout_verified else 'not verified (no GUIDs restored from file names)'}",
            f"original GUIDs    : {result.guids_restored} restored",
            f"original paths    : {result.paths_restored} restored from the catalog, {result.catalog_conflicts} conflicts",
            f"catalog scenes    : {result.scenes_restored} moved to their original path",
            f"GUID-named files  : {result.guid_named_restored} (shaders and others) moved to their catalog path",
            f"merged duplicates : {result.duplicates_merged} identical copies dropped in favour of the catalog asset",
            *(summarize_atlas(result.atlas_split) if result.atlas_split else []),
            f"output            : {result.out_dir}/project (remapped copy), mapping.json, report.json"]
