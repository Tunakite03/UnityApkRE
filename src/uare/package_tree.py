"""Full asset export of a Unity build: one canonical copy per object, and scene, prefab and spine folders
that hard-link into it.

    shared/<category>/<group>/            canonical copy of every exported object: sprites, textures, fonts,
                                          audio, meshes, materials, shaders, textassets, monobehaviours, ...
    shared/sprite_atlases/<group>/        SpriteAtlas definitions
    evidence/bundles/<group>/             AssetBundle and PreloadData indexes (container lists)
    evidence/unclassified/objects/        type-tree JSON for objects that have no dedicated exporter
    evidence/unclassified/hierarchies/    GameObject trees that are neither a scene nor a prefab
    scenes/<SceneName>/                   scene.json (hierarchy), manifest.json and hard links into shared/
    prefabs/<group>/<PrefabName>/         prefab.json (that prefab's root tree), manifest.json, hard links
    spines/<group>/<SpineName>/           atlas, skeleton and page images (hard links), SkeletonDataAsset dump
    scripts/monoscripts.tsv               MonoScript table: class, namespace, assembly
    scripts/hotdll/                       assemblies stored as TextAsset
    catalog.json, index.html, report.json, game_model.json

Scene, prefab and spine folders hold hard links, not copies: each link is the same inode as its shared file.

The group of an object is taken from its source bundle name: YooAsset names encode the asset path,
so the first four tokens (after `gamepackage_`, without the content hash) keep related assets
together. FMOD sound banks (a TextAsset starting with `RIFF....FEV `) are copied as `.bank` and each
FSB5 sample container inside them is decoded to WAV with fmod_toolkit, into `<bank>/`.

Nothing is decrypted. A bundle whose UnityFS magic sits behind a zero prefix is read from the magic on.
"""

import importlib
import json
import logging
import os
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from uare.analyze import Layout, resolve_layout
from uare.blueprint import ScopeSpec, classify_scopes
from uare.errors import UareError
from uare.logs import event
from uare.game_model import (FileRecord, ObjectRecord, Reference, closure, identity, outgoing, resolve,
                             save_model)
from uare.model_capture import UnityObject, capture, group_objects, serialized_name
from uare.outdir import staged_output
from uare.unity import _SIGNATURE_SCAN, bundle_offset

LOG = logging.getLogger("uare.package_tree")

_SANITIZE = re.compile(r"[^A-Za-z0-9._-]+")
_HASH_TAIL = re.compile(r"_[0-9a-f]{20,}$")
_YOOASSET_PREFIXES = (
    "share_assets_artassets_",
    "share_assets_gameassets_",
    "assets_gameassets_",
    "assets_artassets_",
    "assets_scripts_",
    "assets_",
)
_TREE_OUTPUT_PREFIX = ".trees/"
_MAX_STEM = 120
_MAX_DEPTH = 64
_SPINE_TEXT_SUFFIXES = (".atlas", ".skel", ".skel.bytes")
_SPINE_SCRIPT_MARKERS = ("Skeleton", "Spine")
_FAILURES = (EOFError, ValueError, OSError, KeyError, TypeError, AttributeError,
             NotImplementedError, RuntimeError, IndexError)
_HIERARCHY_CLASSES = frozenset({"GameObject", "Transform", "RectTransform"})

# UnityPy class name -> output folder under shared/. Classes not listed here get a type-tree JSON
# under evidence/unclassified/objects/ unless they are attached to a GameObject, in which case they are
# stored in the prefab or scene JSON. Bundle indexes are evidence and go to evidence/bundles/.
_CATEGORY = {
    "Sprite": "sprites", "Texture2D": "textures", "Font": "fonts", "AudioClip": "audio",
    "VideoClip": "videos", "AnimationClip": "animations", "Animation": "animations",
    "AnimatorController": "animators", "AnimatorOverrideController": "animators",
    "Mesh": "meshes", "Material": "materials", "Shader": "shaders", "TextAsset": "textassets",
    "MonoBehaviour": "monobehaviours", "AssetBundle": "bundles", "PreloadData": "bundles",
    "SpriteAtlas": "sprite_atlases", "AssetBundleManifest": "bundles",
}
_TYPETREE_CLASSES = frozenset({
    "Material", "Shader", "AnimationClip", "Animation", "AnimatorController",
    "AnimatorOverrideController", "AssetBundle", "PreloadData", "SpriteAtlas",
    "AssetBundleManifest", "MonoBehaviour", "VideoClip",
})


class UnityPyRequired(UareError):
    """The asset export needs UnityPy, Pillow and fmod_toolkit; install the `power` extra."""


@dataclass(frozen=True)
class CatalogEntry:
    path_id: int
    class_name: str
    name: str
    category: str
    source: str
    output: str | None
    bytes: int
    note: str | None
    object_id: str = ""
    serialized_file: str = ""


@dataclass(frozen=True)
class PackageResult:
    root: str
    files: int
    objects: int
    exported: int
    in_hierarchy: int
    skipped: int
    categories: dict[str, int]
    hotdll: int
    scenes: tuple[str, ...]
    errors: int
    prefabs: int
    spines: int
    links: int
    most_links_per_file: int
    issues: tuple[str, ...]


class PackageLinkError(UareError):
    """Scene and prefab folders are hard links into shared/; the output volume must support them."""


class PackageLayoutError(UareError):
    """An exported object could not be placed in a scene, prefab or evidence file."""


def require_unitypy() -> None:
    """Fail before any work starts when the asset export cannot run."""
    for module in ("UnityPy", "PIL", "fmod_toolkit"):
        try:
            importlib.import_module(module)
        except ImportError as exc:
            raise UnityPyRequired(
                f"the asset export needs {module} ({exc}); install it with `pip install 'uare[power]'`, "
                "or set UARE_PYTHON to an interpreter that has it") from exc


def _sanitize(text: str, limit: int = _MAX_STEM) -> str:
    return _SANITIZE.sub("_", text).strip("_.")[:limit] or "unnamed"


def _group_of(path: Path, is_bundle: bool) -> str:
    if not is_bundle:
        return "_serialized"
    stem = _HASH_TAIL.sub("", path.stem)
    tokens = [t for t in stem.split("_") if t]
    if tokens[:1] == ["gamepackage"]:
        tokens = tokens[1:]
    joined = "_".join(tokens)
    for prefix in _YOOASSET_PREFIXES:
        if joined.startswith(prefix):
            joined = joined[len(prefix):]
            break
    parts = [_sanitize(p, 40) for p in joined.split("_") if p]
    if not parts:
        return "_misc"
    return "/".join(parts[:4])


def _trim_group(group: str, category: str) -> str:
    """Strip a leading group segment when it duplicates the category name."""
    head, sep, tail = group.partition("/")
    if head == category and tail:
        return tail
    return group


def _unique(base: str, path_id: int, used: set[str]) -> str:
    """Return `base` unless taken; collisions get a sequential counter before the extension."""
    ext = Path(base).suffix
    root = base[:-len(ext)] if ext else base
    candidate = base
    counter = 1
    while candidate.casefold() in used:
        candidate = f"{root}_{counter}{ext}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def _as_bytes(value: Any) -> bytes:
    """Raw bytes of a binary payload. A list of byte values is converted, never stringified."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", errors="surrogateescape")
    if isinstance(value, (list, tuple)) and all(isinstance(b, int) and 0 <= b <= 255 for b in value):
        return bytes(value)
    raise TypeError(f"binary payload has unsupported type {type(value).__name__}")


def _class_name(obj: Any) -> str:
    return getattr(getattr(obj, "type", None), "name", None) or str(obj.type)


def _object_name(obj: Any) -> str:
    try:
        value = obj.peek_name()
    except _FAILURES:
        return ""
    return value if isinstance(value, str) else ""


def _pid(ref: Any) -> int:
    return int(getattr(ref, "path_id", 0) or 0)


def _script_name(obj: Any) -> str:
    """Class name of a MonoBehaviour's script, resolved through its MonoScript pointer."""
    try:
        script = obj.read().m_Script
        return str(getattr(script.read(), "m_ClassName", "") or "")
    except _FAILURES:
        return ""


def _write(target: Path, payload: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def _riff_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if len(data) < 12 or data[:4] != b"RIFF":
        return []
    chunks: list[tuple[bytes, bytes]] = []
    offset = 12
    while offset + 8 <= len(data):
        tag = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        chunks.append((tag, data[offset + 8:offset + 8 + size]))
        offset += 8 + size + (size & 1)
    return chunks


def _is_fev_bank(payload: bytes) -> bool:
    return payload[:4] == b"RIFF" and payload[8:12] == b"FEV "


def _fsb_blobs(bank: bytes) -> list[bytes]:
    """FSB5 sample containers stored in the SND chunks of an FMOD Studio bank."""
    blobs: list[bytes] = []
    for tag, body in _riff_chunks(bank):
        if tag != b"SND ":
            continue
        starts: list[int] = []
        index = body.find(b"FSB5")
        while index >= 0:
            starts.append(index)
            index = body.find(b"FSB5", index + 1)
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(body)
            blobs.append(body[start:end])
    return blobs


def _export_fev_bank(payload: bytes, directory: Path, stem: str) -> tuple[Path, str | None]:
    bank_name = stem if stem.endswith(".bank") else f"{stem}.bank"
    bank = _write(directory / bank_name, payload)
    blobs = _fsb_blobs(payload)
    if not blobs:
        return bank, "FMOD bank without FSB5 sample data (event metadata only)"
    fmod_toolkit = importlib.import_module("fmod_toolkit")
    sample_stem = Path(bank_name).stem
    decoded = 0
    for index, blob in enumerate(blobs):
        subsounds = fmod_toolkit.raw_to_wav(blob, f"{sample_stem}-{index}", 2, 44100)
        for sub_name, wav in subsounds.items():
            prefix = f"{index:02d}_" if len(blobs) > 1 else ""
            _write(directory / sample_stem / f"{prefix}{_sanitize(Path(sub_name).stem)}.wav", bytes(wav))
            decoded += 1
    if not decoded:
        return bank, "FSB5 present but no subsounds decoded"
    return bank, f"{decoded} subsounds decoded into {sample_stem}/"


def _export_typetree_json(obj: Any, stem: str, directory: Path) -> tuple[Path | None, str | None]:
    try:
        payload = _json_bytes(obj.read_typetree())
    except _FAILURES as exc:
        return None, f"no type tree ({type(exc).__name__}: {exc})"
    return _write(directory / f"{stem}.json", payload), None


def _export_object(obj: Any, cls: str, directory: Path, stem: str) -> tuple[Path | None, str | None]:
    """Dedicated exporter for a class in _CATEGORY. Returns (output, reason when there is none)."""
    data = obj.read()
    if cls in ("Texture2D", "Sprite"):
        if cls == "Texture2D" and (data.m_Width == 0 or data.m_Height == 0):
            return None, "empty placeholder texture (0x0)"
        try:
            image = data.image
        except OSError as exc:
            return None, f"no image data in bundle ({type(exc).__name__})"
        if image is None:
            return None, "no decodable image"
        target = directory / f"{stem}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target)
        return target, None
    if cls == "AudioClip":
        samples = [(name, payload) for name, payload in (data.samples or {}).items() if payload]
        if not samples:
            return None, "no decodable samples"
        first: Path | None = None
        for index, (sample_name, payload) in enumerate(samples):
            suffix = Path(sample_name).suffix or ".wav"
            target = _write(directory / f"{stem}{'' if index == 0 else f'_{index}'}{suffix}", bytes(payload))
            first = first or target
        return first, None
    if cls == "Font":
        payload = _as_bytes(getattr(data, "m_FontData", b""))
        if not payload:
            return None, "dynamic font without embedded data"
        ext = ".otf" if payload[:4] == b"OTTO" else ".ttf"
        return _write(directory / f"{stem}{ext}", payload), None
    if cls == "Mesh":
        payload = _as_bytes(data.export()) if hasattr(data, "export") else b""
        if not payload:
            return None, "mesh has no exportable vertex data"
        return _write(directory / f"{stem}.obj", payload), None
    if cls == "TextAsset":
        payload = _as_bytes(getattr(data, "m_Script", b""))
        if Path(stem).suffix:
            target = directory / stem
        else:
            try:
                payload.decode("utf-8")
                suffix = ".txt"
            except UnicodeDecodeError:
                suffix = ".dll" if payload[:2] == b"MZ" else ".bin"
            target = directory / f"{stem}{suffix}"
        return _write(target, payload), None
    if cls in _TYPETREE_CLASSES:
        return _export_typetree_json(obj, stem, directory)
    return None, "no exporter for this class"


def _component_record(cpid: int, by_id: dict[int, Any], exported: dict[int, str],
                      homed: set[int]) -> dict[str, Any]:
    homed.add(cpid)
    obj = by_id.get(cpid)
    if obj is None:
        return {"path_id": cpid, "error": "component is not stored in this file"}
    cls = _class_name(obj)
    record: dict[str, Any] = {"class": cls, "path_id": cpid}
    if cpid in exported:
        record["file"] = exported[cpid]
    if cls == "MonoBehaviour":
        record["script"] = _script_name(obj)
    try:
        record["data"] = obj.read_typetree()
    except _FAILURES as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def _build_hierarchy(objs: list[Any], exported: dict[int, str]
                     ) -> tuple[list[dict[str, Any]], set[int], dict[int, str]]:
    """Nested GameObject trees of one file. Returns (roots, path ids placed in the trees, read failures)."""
    by_id = {obj.path_id: obj for obj in objs}
    names: dict[int, str] = {}
    components: dict[int, list[int]] = {}
    transforms: dict[int, tuple[int, int]] = {}
    failures: dict[int, str] = {}
    for obj in objs:
        cls = _class_name(obj)
        if cls == "GameObject":
            try:
                data = obj.read()
                components[obj.path_id] = [_pid(getattr(item, "component", item)) for item in data.m_Component]
                names[obj.path_id] = str(getattr(data, "m_Name", "") or "")
            except _FAILURES as exc:
                failures[obj.path_id] = f"GameObject read failed: {type(exc).__name__}: {exc}"
        elif cls in ("Transform", "RectTransform"):
            try:
                data = obj.read()
                transforms[obj.path_id] = (_pid(data.m_GameObject), _pid(data.m_Father))
            except _FAILURES as exc:
                failures[obj.path_id] = f"{cls} read failed: {type(exc).__name__}: {exc}"

    children: dict[int, list[int]] = defaultdict(list)
    roots: list[int] = []
    for tpid, (_go, father) in transforms.items():
        if father in transforms:
            children[father].append(tpid)
        else:
            roots.append(tpid)

    homed: set[int] = set()
    reached: set[int] = set()

    def node(tpid: int, depth: int) -> dict[str, Any]:
        go, _father = transforms[tpid]
        reached.add(go)
        homed.update({tpid, go})
        pieces = [_component_record(c, by_id, exported, homed) for c in components.get(go, []) if c]
        kids = [node(c, depth + 1) for c in sorted(children[tpid])] if depth < _MAX_DEPTH else []
        return {"name": names.get(go, ""), "gameobject": go, "transform": tpid,
                "components": pieces, "children": kids}

    tree = [node(t, 0) for t in sorted(roots)]
    for go in sorted(set(components) - reached):  # GameObjects without a Transform
        homed.add(go)
        pieces = [_component_record(c, by_id, exported, homed) for c in components[go] if c]
        tree.append({"name": names.get(go, ""), "gameobject": go, "transform": None,
                     "components": pieces, "children": []})
    return tree, homed, failures


def _candidates(layout: Layout) -> list[Path]:
    """Asset bundles plus serialized files (level*, sharedassets*.assets, globalgamemanagers, ...)."""
    search = [layout.data_dir] + ([layout.assets_dir] if layout.assets_dir is not None else [])
    found: set[Path] = set()
    for directory in search:
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                head = handle.read(_SIGNATURE_SCAN)
            if bundle_offset(head) is not None:
                found.add(path)
            elif path.suffix in (".assets", ".unity") or (path.suffix == "" and path.parent == layout.data_dir):
                found.add(path)
    return sorted(found)


def _node_pids(node: dict[str, Any]) -> list[int]:
    """Path ids of every GameObject, Transform and component in one GameObject subtree."""
    pids: list[int] = []
    pending = [node]
    while pending:
        current = pending.pop()
        for key in ("gameobject", "transform"):
            if isinstance(current.get(key), int):
                pids.append(current[key])
        pids.extend(component["path_id"] for component in current.get("components", []))
        pending.extend(current.get("children", []))
    return pids


def _placed(rel: str, serialized: str, roots: list[dict[str, Any]], target: str) -> dict[str, str]:
    """Object id -> final JSON path for every object stored in these roots."""
    return {identity(rel, serialized, pid): target for node in roots for pid in _node_pids(node)}


def _scope_payload(spec: ScopeSpec, roots: list[dict[str, Any]]) -> bytes:
    return _json_bytes({"scope_id": spec.scope_id, "kind": spec.kind, "name": spec.name,
                        "source": spec.source, "serialized_file": spec.serialized_file, "roots": roots})


def _ref(edges: dict[str, tuple[Reference, ...]], oid: str, field: str) -> str | None:
    return next((r.target_id for r in edges.get(oid, ()) if r.field == field), None)


def _atlas_pages(text: str) -> list[str]:
    """Page image names of a Spine atlas: every page block is a name line followed by a `size:` line."""
    pages: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) >= 2 and lines[1].startswith("size:"):
            pages.append(lines[0])
    return pages


def _link(source: Path, target: Path) -> None:
    """Hard-link one canonical file. Never copies and never overwrites: any failure is loud."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError as exc:
        raise PackageLinkError(f"hard link failed ({exc.strerror}); source={source}, target={target}") from exc


def _require_hard_links(staging: Path) -> None:
    """Fail before any export when the output volume cannot create hard links."""
    source, target = staging / ".link-probe-a", staging / ".link-probe-b"
    try:
        source.write_bytes(b"")
        os.link(source, target)
    except OSError as exc:
        raise PackageLinkError(f"scene and prefab folders need hard links; the output volume refused ({exc}); "
                               f"output={staging}") from exc
    finally:
        source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def _link_limit(staging: Path) -> int:
    try:
        return os.pathconf(staging, "PC_LINK_MAX")
    except (OSError, ValueError) as exc:
        raise PackageLinkError(f"cannot read the hard-link limit of the output volume ({exc}); output={staging}") from exc


class _Packager:
    """Writes one output tree. Holds the staging directory, the catalog and the name registry."""

    def __init__(self, unitypy: Any, root: Path, staging: Path) -> None:
        self._unitypy = unitypy
        self._root = root
        self._staging = staging
        self._scratch = staging / ".scratch" / "bundle.bin"
        self._used: dict[str, set[str]] = defaultdict(set)
        self._counts: Counter[str] = Counter()
        self._catalog: list[CatalogEntry] = []
        self._groups: dict[str, str] = {}
        self._trees: dict[tuple[str, str], Path] = {}
        self._tree_seq = 0
        self._layout_issues: list[str] = []
        self._spines = 0
        self._files = 0
        self._objects = 0
        self._errors = 0
        self._hotdll = 0
        self._exported = 0
        self._in_hierarchy = 0
        self._model_files: list[FileRecord] = []
        self._model_objects: list[ObjectRecord] = []
        self._model_issues: list[str] = []

    def run(self, candidates: list[Path]) -> None:
        with (self._staging / "scripts" / "monoscripts.tsv").open("w", encoding="utf-8") as table:
            table.write("file\tpath_id\tname\tclass\tnamespace\tassembly\n")
            self._table = table
            for path in candidates:
                self._package_file(path)

    def finish(self, root: Path) -> PackageResult:
        write = self._staging
        model = resolve(tuple(self._model_files), tuple(self._model_objects), tuple(self._model_issues))
        by_id = {o.id: o for o in model.objects}
        edges = outgoing(model)
        specs = classify_scopes(model, by_id, edges)
        folders = self._scope_folders(specs)
        placements = self._place_hierarchies(specs, folders, by_id)
        self._resolve_component_outputs(placements)
        links, most_links = self._link_scopes(specs, folders, edges)
        self._write_spines(by_id, edges)
        save_model(write / "game_model.json", model)
        (write / "catalog.json").write_text(json.dumps([asdict(e) for e in self._catalog], ensure_ascii=False),
                                            encoding="utf-8")
        (write / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
        categories = {k: v for k, v in self._counts.most_common() if not k.startswith("_")}
        result = PackageResult(
            root=str(root), files=self._files, objects=self._objects, exported=self._exported,
            in_hierarchy=self._in_hierarchy, skipped=self._counts["_skipped"], categories=categories,
            hotdll=self._hotdll,
            scenes=tuple(sorted(folders[s.scope_id].rsplit("/", 1)[-1] for s in specs if s.kind == "scene")),
            errors=self._errors, prefabs=sum(1 for s in specs if s.kind == "prefab"), spines=self._spines,
            links=links, most_links_per_file=most_links, issues=tuple(self._layout_issues))
        (write / "report.json").write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False),
                                           encoding="utf-8")
        shutil.rmtree(self._staging / ".scratch", ignore_errors=True)
        shutil.rmtree(self._staging / ".trees", ignore_errors=True)
        return result

    def _write_json(self, relative: str, payload: bytes) -> None:
        _write(self._staging / relative, payload)

    def _scope_folders(self, specs: list[ScopeSpec]) -> dict[str, str]:
        """Folder of every scene (scenes/<name>) and prefab (prefabs/<group>/<name>), unique within its parent."""
        folders: dict[str, str] = {}
        for spec in sorted(specs, key=lambda s: s.scope_id):
            if spec.kind == "scene":
                stem = Path(spec.name).stem if spec.name.endswith(".unity") else spec.name
                parent = "scenes"
            elif spec.kind == "prefab":
                stem = Path(spec.name).stem
                parent = "prefabs/" + self._groups.get(spec.source, "_serialized")
            else:
                continue
            name = _unique(_sanitize(stem), 0, self._used[parent])
            folders[spec.scope_id] = f"{parent}/{name}"
        return folders

    def _place_hierarchies(self, specs: list[ScopeSpec], folders: dict[str, str],
                           by_id: dict[str, ObjectRecord]) -> dict[str, str]:
        """Write scene, prefab and unclaimed hierarchy JSON. Returns object id -> final JSON path."""
        scenes = {(s.source, s.serialized_file): s for s in specs if s.kind == "scene"}
        prefabs: dict[tuple[str, str], list[ScopeSpec]] = defaultdict(list)
        for spec in specs:
            if spec.kind == "prefab":
                prefabs[spec.source, spec.serialized_file].append(spec)
        placements: dict[str, str] = {}
        written: set[str] = set()
        for (rel, serialized), staged in sorted(self._trees.items()):
            roots: list[dict[str, Any]] = json.loads(staged.read_bytes())["roots"]
            scene = scenes.get((rel, serialized))
            if scene is not None:
                target = folders[scene.scope_id] + "/scene.json"
                self._write_json(target, _scope_payload(scene, roots))
                placements.update(_placed(rel, serialized, roots, target))
                written.add(scene.scope_id)
                continue
            claimed: set[int] = set()
            for spec in prefabs.get((rel, serialized), []):
                pid = by_id[spec.root_id].path_id
                index = next((i for i, node in enumerate(roots)
                              if i not in claimed and node.get("gameobject") == pid), None)
                if index is None:
                    self._layout_issues.append(f"prefab root not found in its hierarchy; scope={spec.scope_id}")
                    continue
                claimed.add(index)
                target = folders[spec.scope_id] + "/prefab.json"
                self._write_json(target, _scope_payload(spec, [roots[index]]))
                placements.update(_placed(rel, serialized, [roots[index]], target))
                written.add(spec.scope_id)
            leftover = [node for i, node in enumerate(roots) if i not in claimed]
            if leftover:
                group = self._groups.get(rel, "_serialized")
                name = _unique(_sanitize(Path(serialized).stem) + ".json", 0, self._used["unclaimed/" + group])
                target = f"evidence/unclassified/hierarchies/{group}/{name}"
                self._write_json(target, _json_bytes({"source": rel, "serialized_file": serialized,
                                                      "roots": leftover}))
                placements.update(_placed(rel, serialized, leftover, target))
        for spec in specs:
            if spec.kind in ("scene", "prefab") and spec.scope_id not in written:
                # Scenes without GameObjects (e.g. Empty.unity) and prefabs whose root is missing keep an empty file.
                file_name = "scene.json" if spec.kind == "scene" else "prefab.json"
                self._write_json(f"{folders[spec.scope_id]}/{file_name}", _scope_payload(spec, []))
        return placements

    def _resolve_component_outputs(self, placements: dict[str, str]) -> None:
        """Point every staged hierarchy row at the JSON file that finally holds it."""
        rewritten: list[CatalogEntry] = []
        for entry in self._catalog:
            if entry.output and entry.output.startswith(_TREE_OUTPUT_PREFIX):
                final = placements.get(entry.object_id)
                if final is None:
                    raise PackageLayoutError(f"hierarchy object has no final location; object={entry.object_id}, "
                                             f"source={entry.source}")
                entry = replace(entry, output=final)
            rewritten.append(entry)
        self._catalog = rewritten

    def _write_spines(self, by_id: dict[str, ObjectRecord], edges: dict[str, tuple[Reference, ...]]) -> None:
        """One folder per SkeletonDataAsset: atlas, skeleton, page images and dump, all hard-linked from shared/."""
        catalog = {entry.object_id: entry for entry in self._catalog}
        images: dict[tuple[str, str], list[str]] = defaultdict(list)
        for entry in self._catalog:
            if (entry.output and entry.output.endswith(".png") and
                    entry.output.startswith(("shared/textures/", "shared/sprites/"))):
                images[self._groups.get(entry.source, "_serialized"), Path(entry.output).name.lower()].append(entry.output)

        def output_of(target: str | None) -> str | None:
            entry = catalog.get(target) if target else None
            return entry.output if entry else None

        for oid in sorted(by_id):
            anchor = by_id[oid]
            script = by_id.get(_ref(edges, oid, "m_Script") or "")
            if anchor.class_name != "MonoBehaviour" or script is None or script.data.get("m_ClassName") != "SkeletonDataAsset":
                continue
            full_group = self._groups.get(anchor.source, "_serialized")
            folder_group = _trim_group(full_group, "spines")
            name = _unique(_sanitize(anchor.name or f"SkeletonDataAsset_{anchor.path_id}"), 0,
                           self._used["spines/" + folder_group])
            folder = f"spines/{folder_group}/{name}"
            dump = output_of(oid)
            skeleton = output_of(_ref(edges, oid, "skeletonJSON"))
            atlases = [output_of(_ref(edges, r.target_id, "atlasFile")) for r in edges.get(oid, ())
                       if r.field.startswith("atlasAssets[") and r.target_id]
            atlas_outputs = [output for output in atlases if output]
            pages: list[str] = []
            for output in atlas_outputs:
                text = (self._staging / output).read_text(encoding="utf-8", errors="replace")
                pages.extend(page for page in _atlas_pages(text) if page not in pages)
            page_rows: list[dict[str, Any]] = []
            for page in pages:
                # Exported names are sanitized object names, so the page name is compared in the same form.
                key = (_sanitize(Path(page).stem) + Path(page).suffix).lower()
                candidates = sorted(images.get((full_group, key), []))
                status = "linked" if len(candidates) == 1 else ("unresolved" if not candidates else "ambiguous")
                page_rows.append({"name": page, "status": status, "shared": candidates[0] if status == "linked" else None,
                                  "candidates": candidates})
            links: dict[str, str] = {}
            for link_name, output in [(Path(o).name, o) for o in [dump, skeleton, *atlas_outputs] if o] + \
                    [(row["name"], row["shared"]) for row in page_rows if row["shared"]]:
                if links.setdefault(link_name, output) != output:
                    raise PackageLayoutError(f"spine folder {folder} has two files named {link_name}")
            for link_name, output in sorted(links.items()):
                _link(self._staging / output, self._staging / folder / link_name)
            self._write_json(f"{folder}/manifest.json", _json_bytes({
                "anchor": {"object_id": oid, "name": anchor.name, "source": anchor.source,
                           "serialized_file": anchor.serialized_file, "output": dump},
                "skeleton": skeleton, "atlases": atlas_outputs, "pages": page_rows,
                "limitations": "Page images are matched by file name inside the same bundle group; "
                               "runtime loading is not observed.",
            }))
            self._spines += 1

    def _link_scopes(self, specs: list[ScopeSpec], folders: dict[str, str],
                     edges: dict[str, tuple[Reference, ...]]) -> tuple[int, int]:
        """Hard-link each scene's and prefab's shared assets into its folder and write its manifest."""
        catalog = {entry.object_id: entry for entry in self._catalog}
        plans: list[tuple[str, dict[str, Any]]] = []
        for spec in specs:
            folder = folders.get(spec.scope_id)
            if folder is None:
                continue
            reachable = closure(set(spec.object_ids), edges)
            assets: dict[str, dict[str, Any]] = {}
            outputs: set[str] = set()
            missing: list[dict[str, Any]] = []
            for oid in sorted(reachable):
                entry = catalog.get(oid)
                output = entry.output if entry else None
                if output is None:
                    missing.append({"object_id": oid, "class_name": entry.class_name if entry else "",
                                    "note": entry.note if entry else "object is not in the catalog"})
                elif output.startswith("shared/"):
                    assets.setdefault(output, {"path": output[len("shared/"):], "shared": output,
                                               "object_id": oid, "class_name": entry.class_name})
                elif not output.startswith(folder + "/"):
                    outputs.add(output)
            unresolved = sum(1 for oid in reachable for r in edges.get(oid, ()) if r.status not in {"resolved", "null"})
            manifest = {
                "scope_id": spec.scope_id, "kind": spec.kind, "name": spec.name, "source": spec.source,
                "serialized_file": spec.serialized_file,
                "assets": sorted(assets.values(), key=lambda a: a["path"]),
                "references": sorted(outputs), "without_output": missing,
                "unresolved_references": unresolved,
                "limitations": "Static reference closure only; dynamic loads, remote content and "
                               "runtime-selected assets are not covered.",
            }
            plans.append((folder, manifest))
        per_file: Counter[str] = Counter(a["shared"] for _folder, manifest in plans for a in manifest["assets"])
        most = max(per_file.values(), default=0)
        limit = _link_limit(self._staging)
        if limit > 0 and most > limit:
            raise PackageLinkError(f"a shared file needs {most} links but the output volume allows {limit}")
        links = 0
        for folder, manifest in plans:
            for asset in manifest["assets"]:
                _link(self._staging / asset["shared"], self._staging / folder / asset["path"])
                links += 1
            self._write_json(f"{folder}/manifest.json", _json_bytes(manifest))
        return links, most

    def _package_file(self, path: Path) -> None:
        rel = path.relative_to(self._root).as_posix()
        raw = path.read_bytes()
        offset = bundle_offset(raw[:_SIGNATURE_SCAN])
        group = _group_of(path, offset is not None)
        self._groups[rel] = group
        try:
            env = self._open(path, raw, offset)
            objs = list(env.objects)
        except Exception as exc:  # UnityPy raises library-specific errors; the file is reported, not dropped
            self._errors += 1
            self._model_issues.append(f"Container unreadable: {rel} ({type(exc).__name__})")
            event(LOG, logging.WARNING, "package_open_failed", path=rel, error=str(exc))
            return
        try:
            self._files += 1
            self._objects += len(objs)
            if not objs and offset is None:
                self._model_files.append(FileRecord(rel, path.name, ()))
            for serialized, scoped_objects in sorted(group_objects(objs, rel).items()):
                self._package_objects(rel, group, serialized, scoped_objects)
        finally:
            self._scratch.unlink(missing_ok=True)

    def _package_objects(self, rel: str, group: str, serialized: str,
                         objs: list[UnityObject]) -> None:
        exported: dict[int, str] = {}
        pending: list[UnityObject] = []
        for obj in objs:
            try:
                cls = _class_name(obj)
                if cls == "MonoScript":
                    self._write_monoscript_row(rel, obj)
                    continue
                category = self._category(obj, cls)
                if category is None:
                    pending.append(obj)
                    continue
                if category == "scripts":
                    directory = self._staging / "scripts" / "hotdll"
                elif category == "bundles":
                    directory = self._staging / "evidence" / "bundles" / group
                else:
                    directory = self._staging / "shared" / category / _trim_group(group, category)
                stem = _unique(_sanitize(_object_name(obj) or f"{cls}_{obj.path_id}"), obj.path_id,
                               self._used[str(directory)])
                if cls == "TextAsset" and category == "audio":
                    target, note = _export_fev_bank(_as_bytes(getattr(obj.read(), "m_Script")), directory, stem)
                else:
                    target, note = _export_object(obj, cls, directory, stem)
            except _FAILURES as exc:
                self._record(rel, obj, "unclassified", None, f"{type(exc).__name__}: {exc}")
                continue
            except Exception as exc:  # one object must not stop the run; its reason goes to the catalog
                self._record(rel, obj, "unclassified", None, f"unexpected {type(exc).__name__}: {exc}")
                continue
            if target is not None:
                exported[obj.path_id] = target.relative_to(self._staging).as_posix()
            self._record(rel, obj, category, target, note)

        tree, homed, failures = _build_hierarchy(objs, exported)
        hierarchy_output = self._stage_hierarchy(serialized, rel, group, tree) if tree else None
        for obj in pending:
            self._record_pending(rel, obj, group, homed, hierarchy_output, failures)
        file_record, object_records = capture(rel, serialized, objs, exported)
        self._model_files.append(file_record)
        self._model_objects.extend(object_records)

    def _open(self, path: Path, raw: bytes, offset: int | None) -> Any:
        if offset is None:
            return self._unitypy.load(str(path))
        self._scratch.parent.mkdir(parents=True, exist_ok=True)
        self._scratch.write_bytes(raw[offset:])
        return self._unitypy.load(str(self._scratch))

    def _category(self, obj: Any, cls: str) -> str | None:
        if cls == "TextAsset":
            payload = _as_bytes(obj.read().m_Script)
            name = _object_name(obj).lower()
            if _is_fev_bank(payload):
                return "audio"
            if name.endswith(".dll") or payload[:2] == b"MZ":
                return "scripts"
            if name.endswith(_SPINE_TEXT_SUFFIXES):
                return "spines"
            return "textassets"
        if cls == "MonoBehaviour":
            script = _script_name(obj)
            if any(marker in script for marker in _SPINE_SCRIPT_MARKERS):
                return "spines"
            return "monobehaviours"
        return _CATEGORY.get(cls)

    def _write_monoscript_row(self, rel: str, obj: Any) -> None:
        try:
            data = obj.read()
        except _FAILURES as exc:
            self._record(rel, obj, "monoscripts", None, f"read failed: {type(exc).__name__}: {exc}")
            return
        self._table.write(f"{rel}\t{obj.path_id}\t{_object_name(obj)}\t{data.m_ClassName}\t"
                          f"{getattr(data, 'm_Namespace', '')}\t{getattr(data, 'm_AssemblyName', '')}\n")
        self._record(rel, obj, "monoscripts", None, "row in scripts/monoscripts.tsv", output="scripts/monoscripts.tsv")

    def _record(self, rel: str, obj: Any, category: str, target: Path | None, note: str | None,
                output: str | None = None) -> None:
        """One catalog entry per object. `target` is the file written for it, `output` a shared file."""
        size = 0
        if target is not None:
            output = target.relative_to(self._staging).as_posix()
            size = target.stat().st_size
        if output is None:
            self._counts["_skipped"] += 1
        else:
            self._counts[category] += 1
            if category == "scripts":
                self._hotdll += 1
            if category not in ("monoscripts",):
                self._exported += 1
        self._catalog.append(CatalogEntry(obj.path_id, _class_name(obj), _object_name(obj), category, rel,
                                          output, size, note, identity(rel, serialized_name(obj, rel), obj.path_id),
                                          serialized_name(obj, rel)))

    def _stage_hierarchy(self, serialized: str, rel: str, group: str, tree: list[dict[str, Any]]) -> str:
        """Hold one serialized file's GameObject trees until finish() knows which scene or prefab owns each root."""
        self._tree_seq += 1
        target = self._staging / ".trees" / f"{self._tree_seq:06d}.json"
        _write(target, _json_bytes({"source": rel, "serialized_file": serialized, "group": group, "roots": tree}))
        self._trees[rel, serialized] = target
        return _TREE_OUTPUT_PREFIX + target.name

    def _record_pending(self, rel: str, obj: Any, group: str, homed: set[int],
                        hierarchy_output: str | None, failures: dict[int, str]) -> None:
        cls = _class_name(obj)
        if obj.path_id in homed and hierarchy_output is not None:
            self._in_hierarchy += 1
            self._counts["components"] += 1
            self._catalog.append(CatalogEntry(obj.path_id, cls, _object_name(obj), "components", rel,
                                              hierarchy_output, 0, "stored in the hierarchy JSON",
                                              identity(rel, serialized_name(obj, rel), obj.path_id),
                                              serialized_name(obj, rel)))
            return
        if obj.path_id in failures:
            self._record(rel, obj, "unclassified", None, failures[obj.path_id], output=None)
            return
        directory = self._staging / "evidence" / "unclassified" / "objects" / _sanitize(cls, 60) / group
        stem = _unique(_sanitize(_object_name(obj) or f"{cls}_{obj.path_id}"), obj.path_id,
                       self._used[str(directory)])
        target, note = _export_typetree_json(obj, stem, directory)
        if target is not None:
            self._record(rel, obj, "objects", target, "no dedicated exporter; type-tree JSON")
        else:
            self._record(rel, obj, "unclassified", None, note)


_INDEX_HTML = """<!doctype html><meta charset="utf-8"><title>uare catalog</title>
<style>body{font:13px/1.4 -apple-system,Segoe UI,sans-serif;margin:16px}input{padding:6px;width:320px}
table{border-collapse:collapse;width:100%;margin-top:10px}td,th{border:1px solid #ddd;padding:3px 6px;text-align:left}
th{background:#f4f4f4;position:sticky;top:0}img{max-height:48px}.sk{color:#a00}</style>
<h2>uare catalog</h2><input id=q placeholder="filter class / name / source / category (regex)"><span id=n></span>
<table><thead><tr><th>class</th><th>name</th><th>category</th><th>preview</th><th>source</th><th>output / note</th></tr></thead>
<tbody id=b></tbody></table>
<script>
fetch('catalog.json').then(r=>r.json()).then(rows=>{const q=document.getElementById('q'),b=document.getElementById('b'),n=document.getElementById('n');
function render(){const p=rows;let re=null;try{re=q.value?new RegExp(q.value,'i'):null}catch(e){}const f=re?p.filter(x=>re.test(x.name+' '+x.class_name+' '+x.source+' '+x.category)):p;
n.textContent=' '+f.length+' / '+p.length;b.innerHTML=f.slice(0,2000).map(x=>{const prev=x.output&&/\\.(png)$/.test(x.output)?`<img src='${x.output}'>`:'';const out=x.output?`<a href='${x.output}'>${x.output}</a>`:'';return `<tr><td>${x.class_name}</td><td>${x.name||''}</td><td>${x.category}</td><td>${prev}</td><td>${x.source}</td><td>${out} <span class=sk>${x.note||''}</span></td></tr>`}).join('')}
q.oninput=render;render();});
</script>"""


def package(root: Path, out_dir: Path) -> PackageResult:
    """Export every readable asset of an extracted Unity tree into `out_dir` (must not exist yet)."""
    require_unitypy()
    unitypy = importlib.import_module("UnityPy")
    layout = resolve_layout(root)
    candidates = _candidates(layout)
    with staged_output(out_dir) as staging:
        for sub in ("shared", "evidence", "scenes", "scripts/hotdll"):
            (staging / sub).mkdir(parents=True)
        _require_hard_links(staging)
        packager = _Packager(unitypy, layout.root, staging)
        packager.run(candidates)
        result = packager.finish(layout.root)
    event(LOG, logging.INFO, "package_done", files=result.files, objects=result.objects,
          exported=result.exported, in_hierarchy=result.in_hierarchy, skipped=result.skipped, errors=result.errors)
    return result


def summarize(result: PackageResult) -> list[str]:
    return [
        f"root              : {result.root}",
        f"files opened      : {result.files} ({result.errors} open errors)",
        f"objects           : {result.objects}",
        f"exported          : {result.exported} files, {result.in_hierarchy} components in hierarchy JSON, "
        f"skipped {result.skipped} (reasons in catalog.json)",
        f"scenes            : {len(result.scenes)} folders under scenes/",
        f"prefabs           : {result.prefabs} folders under prefabs/",
        f"spines            : {result.spines} folders under spines/",
        f"links             : {result.links} hard links; most links on one shared file = {result.most_links_per_file}",
        f"layout issues     : {len(result.issues)}",
        "categories        : " + ", ".join(f"{k}={v}" for k, v in result.categories.items()),
        "model             : game_model.json (scoped identities, fields, resolved and missing references)",
        "index             : index.html (raw catalog); use `uare blueprint` for scene and system reports",
    ]
