"""Optional UnityPy-backed asset export (the `power` extra).

The stdlib backend (assets.py) reads SerializedFiles by hand and exports TextAsset bytes and a
MonoScript table; it cannot open AssetBundles or decode compressed media. This backend uses
UnityPy (a third-party library, installed via `pip install uare[power]`) to go further:

- opens `assets/aa/**/*.bundle` (UnityFS/legacy) by decompressing LZ4/LZMA internally;
- decodes Texture2D/Sprite to PNG (needs Pillow), uncompressed AudioClip samples to WAV, and Mesh
  to OBJ;
- reads MonoBehaviour fields when a type tree is available (from an embedded type tree or from an
  IL2CPP type-tree generator), so Visual Scripting graph assets can be serialized to JSON.

It is opt-in (`uare export-assets --backend unitypy|auto`). The module imports UnityPy lazily, so
the rest of the toolkit stays standard-library only. Objects UnityPy cannot decode are still
listed and counted, never silently dropped.
"""

import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from uare.analyze import resolve_layout
from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output
from uare.unity import _SIGNATURE_SCAN, FileKind, bundle_offset, detect_kind

LOG = logging.getLogger("uare.unitypy_backend")

_OPEN_KINDS = frozenset({FileKind.SERIALIZED, FileKind.BUNDLE_UNITYFS, FileKind.BUNDLE_LEGACY})
_SANITIZE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM = 150
_MEDIA_CLASSES = frozenset({"Texture2D", "Sprite", "AudioClip", "Mesh", "MonoBehaviour"})


class UnityPyUnavailable(UareError):
    """UnityPy is not installed; install the `power` extra."""


@dataclass(frozen=True)
class UnityPyResult:
    root: str
    files: int
    objects: int
    text_assets: int
    textures: int
    audio: int
    meshes: int
    monobehaviours: int
    errors: int
    class_counts: dict[str, int]


def available() -> bool:
    try:
        import UnityPy  # noqa: F401
    except ImportError:
        return False
    return True


def _require():
    try:
        import UnityPy
    except ImportError as exc:  # pragma: no cover - only hit without the extra
        raise UnityPyUnavailable(
            "UnityPy is not installed; run `pip install uare[power]` or pass --backend stdlib"
        ) from exc
    return UnityPy


def _stem(name: str, path_id: int, used: set[str]) -> str:
    base = _SANITIZE.sub("_", name).strip("_.")[:_MAX_STEM] or f"asset_{path_id}"
    candidate = base
    suffix = path_id
    while candidate.casefold() in used:
        candidate = f"{base}.{suffix}"
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


def _safe_name(obj: Any) -> str:
    try:
        value = obj.peek_name()
    except Exception:  # UnityPy raises many shapes for nameless objects
        return ""
    return value if isinstance(value, str) and value.isprintable() else ""


def _class_name(obj: Any) -> str:
    kind = getattr(obj, "type", None)
    return getattr(kind, "name", None) or str(kind)


def _iter_files(root: "Any") -> list[Path]:
    layout = resolve_layout(root)
    dirs = [layout.data_dir] + ([layout.assets_dir] if layout.assets_dir is not None else [])
    files: list[Path] = []
    for directory in dirs:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if detect_kind(path) in _OPEN_KINDS:
                files.append(path)
                continue
            with path.open("rb") as handle:  # a bundle behind a short header (e.g. YooAsset)
                if bundle_offset(handle.read(_SIGNATURE_SCAN)) is not None:
                    files.append(path)
    return files


def _load(path: Path, unitypy: Any):
    with path.open("rb") as handle:
        head = handle.read(_SIGNATURE_SCAN)
    offset = bundle_offset(head)
    if offset:
        return unitypy.load(path.read_bytes()[offset:])
    return unitypy.load(str(path))


def _export_text_asset(obj: Any, name: str, path_id: int, out_dir: Path, used: set[str]) -> bool:
    data = obj.read()
    payload = getattr(data, "m_Script", None)
    if payload is None:
        return False
    # UnityPy decodes TextAsset bytes with surrogateescape; re-encode the same way so the original
    # bytes round-trip instead of raising on lone surrogates.
    (out_dir / _stem(name or f"TextAsset_{path_id}", path_id, used)).write_bytes(_as_bytes(payload))
    return True


def _export_texture(obj: Any, name: str, path_id: int, out_dir: Path, used: set[str]) -> bool:
    data = obj.read()
    image = getattr(data, "image", None)
    if image is None:
        return False
    target = out_dir / (_stem(name or f"Texture_{path_id}", path_id, used) + ".png")
    image.save(target)
    return True


def _export_audio(obj: Any, name: str, path_id: int, out_dir: Path, used: set[str]) -> int:
    data = obj.read()
    samples = getattr(data, "samples", None) or {}
    written = 0
    for clip_name, payload in samples.items():
        if not payload:
            continue
        suffix = Path(clip_name).suffix or ".wav"
        target = out_dir / _stem(Path(clip_name).stem or name or f"Audio_{path_id}", path_id + written, used)
        target = target.with_suffix(suffix)
        target.write_bytes(_as_bytes(payload))
        written += 1
    return written


def _export_mesh(obj: Any, name: str, path_id: int, out_dir: Path, used: set[str]) -> bool:
    data = obj.read()
    payload = _as_bytes(data.export())
    if not payload:
        return False
    (out_dir / (_stem(name or f"Mesh_{path_id}", path_id, used) + ".obj")).write_bytes(payload)
    return True


def _export_monoscript(obj: Any, rel: str, name: str, path_id: int, handle: Any) -> None:
    try:
        data = obj.read()
        class_name = getattr(data, "m_ClassName", "") or ""
        namespace = getattr(data, "m_Namespace", "") or ""
        assembly = getattr(data, "m_AssemblyName", "") or ""
    except Exception:
        class_name = namespace = assembly = ""
    handle.write(f"{rel}\t{path_id}\t{name}\t{class_name}\t{namespace}\t{assembly}\n")


def _export_monobehaviour(obj: Any, name: str, path_id: int, out_dir: Path, used: set[str]) -> bool:
    try:
        tree = obj.read_typetree()
    except Exception:
        return False
    (out_dir / (_stem(name or f"MonoBehaviour_{path_id}", path_id, used) + ".json")).write_text(
        json.dumps(tree, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return True


def export_assets(root: Path, out_dir: Path) -> UnityPyResult:
    unitypy = _require()
    files = _iter_files(root)
    with staged_output(out_dir) as staging:
        dirs = {key: staging / key for key in ("textassets", "textures", "audio", "meshes", "monobehaviours")}
        for directory in dirs.values():
            directory.mkdir()
        used: dict[str, set[str]] = {key: set() for key in dirs}
        counters = Counter()
        errors = objects = file_count = 0
        with (staging / "objects.jsonl").open("w", encoding="utf-8") as obj_out, \
             (staging / "monoscripts.tsv").open("w", encoding="utf-8") as mono_out:
            for path in files:
                try:
                    env = _load(path, unitypy)
                except Exception as exc:
                    errors += 1
                    event(LOG, logging.WARNING, "unitypy_load_failed", path=str(path), error=str(exc))
                    continue
                file_count += 1
                rel = path.relative_to(resolve_layout(root).root).as_posix()
                for obj in env.objects:
                    objects += 1
                    class_name = _class_name(obj)
                    counters[class_name] += 1
                    name = _safe_name(obj)
                    # `obj.container` triggers a UnityPy container index that can mutate its cab
                    # dict while iterating (RuntimeError on large trees); the source file is enough.
                    record = {
                        "file": rel, "path_id": getattr(obj, "path_id", 0), "class_name": class_name,
                        "name": name, "byte_size": getattr(obj, "byte_size", 0),
                    }
                    obj_out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    try:
                        if class_name == "TextAsset":
                            counters["_text"] += _export_text_asset(obj, name, record["path_id"], dirs["textassets"], used["textassets"])
                        elif class_name in ("Texture2D", "Sprite"):
                            counters["_texture"] += _export_texture(obj, name, record["path_id"], dirs["textures"], used["textures"])
                        elif class_name == "AudioClip":
                            counters["_audio"] += _export_audio(obj, name, record["path_id"], dirs["audio"], used["audio"])
                        elif class_name == "Mesh":
                            counters["_mesh"] += _export_mesh(obj, name, record["path_id"], dirs["meshes"], used["meshes"])
                        elif class_name == "MonoBehaviour":
                            counters["_mono"] += _export_monobehaviour(obj, name, record["path_id"], dirs["monobehaviours"], used["monobehaviours"])
                        elif class_name == "MonoScript":
                            _export_monoscript(obj, rel, name, record["path_id"], mono_out)
                    except Exception as exc:
                        errors += 1
                        event(LOG, logging.WARNING, "unitypy_export_failed", class_name=class_name,
                              path_id=record["path_id"], error=str(exc))

        result = UnityPyResult(
            root=str(resolve_layout(root).root), files=file_count, objects=objects,
            text_assets=counters["_text"], textures=counters["_texture"], audio=counters["_audio"],
            meshes=counters["_mesh"], monobehaviours=counters["_mono"], errors=errors,
            class_counts=dict((k, v) for k, v in counters.most_common() if not k.startswith("_")))
        (staging / "assets_report.json").write_text(
            json.dumps(asdict(result), indent=2, ensure_ascii=False), encoding="utf-8")
    event(LOG, logging.INFO, "unitypy_done", objects=objects, textures=result.textures, audio=result.audio,
          meshes=result.meshes, monobehaviours=result.monobehaviours, errors=errors)
    return result


def summarize(result: UnityPyResult) -> list[str]:
    lines = [
        f"backend           : UnityPy",
        f"root              : {result.root}",
        f"files opened      : {result.files} ({result.errors} object/load errors)",
        f"objects           : {result.objects}",
        f"text assets       : {result.text_assets} -> textassets/",
        f"textures          : {result.textures} -> textures/ (PNG)",
        f"audio clips       : {result.audio} -> audio/",
        f"meshes            : {result.meshes} -> meshes/ (OBJ)",
        f"monobehaviours    : {result.monobehaviours} -> monobehaviours/ (JSON, needs a type tree)",
    ]
    top = list(result.class_counts.items())[:12]
    lines.append("top classes       : " + ", ".join(f"{name} {count}" for name, count in top))
    return lines
