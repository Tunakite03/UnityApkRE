"""Full asset export of a Unity build, grouped by category and by source bundle.

    assets/<category>/<group>/            exported objects: sprites, textures, fonts, audio, meshes, ...
    assets/objects/<class>/<group>/       type-tree JSON for objects that have no dedicated exporter
    assets/prefabs/<group>/<bundle>.json  GameObject trees, each component inline or by file reference
    scenes/<level>/hierarchy.json         scene hierarchies
    scripts/monoscripts.tsv               MonoScript table: class, namespace, assembly
    scripts/hotdll/                       assemblies stored as TextAsset
    catalog.json                          one entry per object read: its output, or why it has none
    index.html                            filterable view of catalog.json
    report.json                           counts and per-file open errors

The group of an object is taken from its source bundle name: YooAsset names encode the asset path,
so the first four tokens (after `gamepackage_`, without the content hash) keep related assets
together. FMOD sound banks (a TextAsset starting with `RIFF....FEV `) are copied as `.bank` and each
FSB5 sample container inside them is decoded to WAV with fmod_toolkit, into `<bank>/`.

Nothing is decrypted. A bundle whose UnityFS magic sits behind a zero prefix is read from the magic on.
"""

import importlib
import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from uare.analyze import Layout, resolve_layout
from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output
from uare.unity import _SIGNATURE_SCAN, bundle_offset

LOG = logging.getLogger("uare.package_tree")

_SANITIZE = re.compile(r"[^A-Za-z0-9._-]+")
_HASH_TAIL = re.compile(r"_[0-9a-f]{20,}$")
_SCENE = re.compile(r"^level\d+$")
_MAX_STEM = 120
_MAX_DEPTH = 64
_SPINE_TEXT_SUFFIXES = (".atlas", ".skel", ".skel.bytes")
_SPINE_SCRIPT_MARKERS = ("Skeleton", "Spine")
_FAILURES = (EOFError, ValueError, OSError, KeyError, TypeError, AttributeError,
             NotImplementedError, RuntimeError, IndexError)
_HIERARCHY_CLASSES = frozenset({"GameObject", "Transform", "RectTransform"})

# UnityPy class name -> output folder under assets/. Classes not listed here get a type-tree JSON
# under assets/objects/ unless they are attached to a GameObject, in which case they are stored in
# the prefab or scene JSON.
_CATEGORY = {
    "Sprite": "sprites", "Texture2D": "textures", "Font": "fonts", "AudioClip": "audio",
    "VideoClip": "videos", "AnimationClip": "animations", "Animation": "animations",
    "AnimatorController": "animators", "AnimatorOverrideController": "animators",
    "Mesh": "meshes", "Material": "materials", "Shader": "shaders", "TextAsset": "textassets",
    "MonoBehaviour": "monobehaviours", "AssetBundle": "manifests", "PreloadData": "manifests",
    "SpriteAtlas": "manifests", "AssetBundleManifest": "manifests",
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
    tokens = [t for t in _HASH_TAIL.sub("", path.stem).split("_") if t]
    if tokens[:1] == ["gamepackage"]:
        tokens = tokens[1:]
    return _sanitize("_".join(tokens[:4]), 60)


def _unique(base: str, path_id: int, used: set[str]) -> str:
    """Return `base` unless taken; a collision gets the path id inserted before the extension."""
    ext = Path(base).suffix
    root = base[:-len(ext)] if ext else base
    candidate = base
    suffix = path_id
    while candidate.casefold() in used:
        candidate = f"{root}.{suffix}{ext}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def _as_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", errors="surrogateescape")
    return str(value).encode("utf-8", errors="surrogateescape")


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
        return record
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
        self._scenes: list[str] = []
        self._files = 0
        self._objects = 0
        self._errors = 0
        self._hotdll = 0
        self._exported = 0
        self._in_hierarchy = 0

    def run(self, candidates: list[Path]) -> None:
        with (self._staging / "scripts" / "monoscripts.tsv").open("w", encoding="utf-8") as table:
            table.write("file\tpath_id\tname\tclass\tnamespace\tassembly\n")
            self._table = table
            for path in candidates:
                self._package_file(path)

    def finish(self, root: Path) -> PackageResult:
        write = self._staging
        (write / "catalog.json").write_text(json.dumps([asdict(e) for e in self._catalog], ensure_ascii=False),
                                            encoding="utf-8")
        (write / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
        categories = {k: v for k, v in self._counts.most_common() if not k.startswith("_")}
        result = PackageResult(root=str(root), files=self._files, objects=self._objects,
                               exported=self._exported, in_hierarchy=self._in_hierarchy,
                               skipped=self._counts["_skipped"], categories=categories,
                               hotdll=self._hotdll, scenes=tuple(sorted(self._scenes)), errors=self._errors)
        (write / "report.json").write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False),
                                           encoding="utf-8")
        shutil.rmtree(self._staging / ".scratch", ignore_errors=True)
        return result

    def _package_file(self, path: Path) -> None:
        rel = path.relative_to(self._root).as_posix()
        raw = path.read_bytes()
        offset = bundle_offset(raw[:_SIGNATURE_SCAN])
        group = _group_of(path, offset is not None)
        try:
            env = self._open(path, raw, offset)
            objs = list(env.objects)
        except Exception as exc:  # UnityPy raises library-specific errors; the file is reported, not dropped
            self._errors += 1
            event(LOG, logging.WARNING, "package_open_failed", path=rel, error=str(exc))
            return
        try:
            self._files += 1
            self._objects += len(objs)
            exported: dict[int, str] = {}
            pending: list[Any] = []
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
                    directory = self._staging / "scripts" / "hotdll" if category == "scripts" \
                        else self._staging / "assets" / category / group
                    stem = _unique(_sanitize(_object_name(obj) or f"{cls}_{obj.path_id}"), obj.path_id,
                                   self._used[str(directory)])
                    if cls == "TextAsset" and category == "audio":
                        target, note = _export_fev_bank(_as_bytes(obj.read().m_Script), directory, stem)
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
            hierarchy_output = self._write_hierarchy(path, rel, group, tree) if tree else None
            for obj in pending:
                self._record_pending(rel, obj, group, homed, hierarchy_output, failures)
        finally:
            self._scratch.unlink(missing_ok=True)

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
                                          output, size, note))

    def _write_hierarchy(self, path: Path, rel: str, group: str, tree: list[dict[str, Any]]) -> str:
        payload = _json_bytes({"source": rel, "roots": tree})
        if _SCENE.match(path.stem):
            self._scenes.append(path.stem)
            target = self._staging / "scenes" / path.stem / "hierarchy.json"
            category = "scenes"
        else:
            directory = self._staging / "assets" / "prefabs" / group
            target = directory / _unique(_sanitize(path.stem) + ".json", 0, self._used[str(directory)])
            category = "prefabs"
        _write(target, payload)
        self._counts[category] += 1
        return target.relative_to(self._staging).as_posix()

    def _record_pending(self, rel: str, obj: Any, group: str, homed: set[int],
                        hierarchy_output: str | None, failures: dict[int, str]) -> None:
        cls = _class_name(obj)
        if obj.path_id in homed and hierarchy_output is not None:
            self._in_hierarchy += 1
            self._counts["components"] += 1
            self._catalog.append(CatalogEntry(obj.path_id, cls, _object_name(obj), "components", rel,
                                              hierarchy_output, 0, "stored in the hierarchy JSON"))
            return
        if obj.path_id in failures:
            self._record(rel, obj, "unclassified", None, failures[obj.path_id], output=None)
            return
        directory = self._staging / "assets" / "objects" / _sanitize(cls, 60) / group
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
        for sub in ("assets", "scenes", "scripts/hotdll"):
            (staging / sub).mkdir(parents=True)
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
        f"exported          : {result.exported} files, {result.in_hierarchy} components in prefab/scene JSON, "
        f"skipped {result.skipped} (reasons in catalog.json)",
        f"scenes            : {', '.join(result.scenes) or '-'}",
        "categories        : " + ", ".join(f"{k}={v}" for k, v in result.categories.items()),
        "index             : index.html (filter by class/name/source), catalog.json",
    ]
