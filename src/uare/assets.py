"""Asset inventory and extraction from an extracted Unity build.

Reliable, stdlib-only outputs:
- objects.jsonl   : every object in every SerializedFile, with class name, name (for named types)
                    and byte size. A complete asset index.
- textassets/     : every TextAsset's bytes, written to a file named after m_Name. These hold the
                    game's text data (JSON, CSV, Lua, configs) and are the highest-value export.
- monoscripts.tsv : index, m_Name, class, namespace, assembly for every MonoScript - ties the
                    asset graph to the IL2CPP class names.
- assets_report.json : per-class counts across the build.

Out of scope here: decoding binary media (Texture2D/AudioClip/Mesh pixel/sample data) needs native
codecs (ETC/ASTC/crunch, Vorbis) that the standard library does not provide. The object index
records those assets and their sizes; use AssetRipper or UnityPy to decode them. AssetBundles
(assets/aa Addressables) are compressed containers; their inner SerializedFile is not unpacked here.
"""

import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from uare.analyze import resolve_layout
from uare.errors import UareError, UnityFormatError
from uare.logs import event
from uare.outdir import staged_output
from uare.serialized import ObjectReader, SerializedFile, parse_serialized_file
from uare.unity import FileKind, detect_kind

LOG = logging.getLogger("uare.assets")

TEXT_ASSET = 49
MONO_SCRIPT = 115

# Class ids seen in Unity player builds. Not exhaustive; unknown ids are reported numerically.
CLASS_IDS: dict[int, str] = {
    1: "GameObject", 2: "Component", 4: "Transform", 8: "Behaviour", 20: "Camera", 21: "Material",
    23: "MeshRenderer", 25: "Renderer", 27: "Texture", 28: "Texture2D", 33: "MeshFilter",
    43: "Mesh", 48: "Shader", 49: "TextAsset", 65: "BoxCollider", 74: "AnimationClip",
    82: "AudioSource", 83: "AudioClip", 89: "Cubemap", 95: "Animator", 108: "Light",
    114: "MonoBehaviour", 115: "MonoScript", 128: "Font", 135: "SphereCollider",
    137: "SkinnedMeshRenderer", 142: "AssetBundle", 150: "PreloadData", 156: "TerrainData",
    198: "ParticleSystem", 199: "ParticleSystemRenderer", 212: "SpriteRenderer", 213: "Sprite",
    222: "CanvasRenderer", 223: "Canvas", 224: "RectTransform", 225: "CanvasGroup",
    687078895: "SpriteAtlas", 850595691: "LightingSettings", 1839735485: "NavMeshData",
}

# Types whose serialized body begins with an aligned m_Name string. Each read is validated, so a
# type that does not actually start with m_Name yields an empty name rather than garbage.
_NAMED_CLASSES = frozenset({21, 28, 43, 48, 49, 74, 83, 89, 115, 128, 213, 687078895})
_MAX_NAME = 512
_SANITIZE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ObjectRecord:
    file: str
    path_id: int
    class_id: int
    class_name: str
    name: str
    byte_size: int


@dataclass(frozen=True)
class MonoScriptRecord:
    file: str
    path_id: int
    name: str
    class_name: str
    namespace: str
    assembly: str


@dataclass(frozen=True)
class AssetsResult:
    root: str
    serialized_files: int
    skipped_bundles: int
    objects: int
    class_counts: dict[str, int]
    text_assets_written: int
    mono_scripts: int


def _clean_name(raw: bytes) -> str:
    if len(raw) > _MAX_NAME:
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return text if text.isprintable() else ""


def _read_name(file: SerializedFile, data: bytes, info, class_id: int) -> str:
    if class_id not in _NAMED_CLASSES:
        return ""
    try:
        return _clean_name(ObjectReader(file, data, info).string())
    except UnityFormatError:
        return ""


def _read_mono_script(file: SerializedFile, data: bytes, info) -> MonoScriptRecord | None:
    # MonoScript: m_Name, m_ExecutionOrder(int), m_PropertiesHash(16), m_ClassName, m_Namespace, m_AssemblyName
    try:
        reader = ObjectReader(file, data, info)
        name = _clean_name(reader.string())
        reader.i32()
        reader.pos += 16
        class_name = _clean_name(reader.string())
        namespace = _clean_name(reader.string())
        assembly = _clean_name(reader.string())
    except (UnityFormatError, AttributeError):
        return None
    return MonoScriptRecord("", info.path_id, name, class_name, namespace, assembly)


_MAX_STEM = 150


def _unique_path(directory: Path, name: str, path_id: int, used: set[str]) -> Path:
    stem = _SANITIZE.sub("_", name).strip("_.")[:_MAX_STEM] or f"asset_{path_id}"
    candidate = stem
    suffix = path_id
    while candidate.casefold() in used:  # case-insensitive: APFS/NTFS fold case
        candidate = f"{stem}.{suffix}"
        suffix += 1
    used.add(candidate.casefold())
    return directory / candidate


def _iter_serialized(data_dir: Path):
    for path in sorted(data_dir.rglob("*")):
        if path.is_file() and detect_kind(path) is FileKind.SERIALIZED:
            yield path


def analyze_assets(root: Path, out_dir: Path) -> AssetsResult:
    layout = resolve_layout(root)
    search_dirs = [layout.data_dir]
    if layout.assets_dir is not None:
        search_dirs.append(layout.assets_dir)

    with staged_output(out_dir) as staging:
        text_dir = staging / "textassets"
        text_dir.mkdir()
        used_names: set[str] = set()
        class_counts: Counter[str] = Counter()
        serialized_files = skipped_bundles = object_total = text_written = mono_total = 0

        for data_dir in search_dirs:
            # Count bundles we are not unpacking, for an honest total.
            skipped_bundles += sum(
                1 for p in data_dir.rglob("*") if p.is_file()
                and detect_kind(p) in (FileKind.BUNDLE_UNITYFS, FileKind.BUNDLE_LEGACY))

        with (staging / "objects.jsonl").open("w", encoding="utf-8") as obj_out, \
             (staging / "monoscripts.tsv").open("w", encoding="utf-8") as mono_out:
            for data_dir in search_dirs:
                for path in _iter_serialized(data_dir):
                    data = path.read_bytes()
                    try:
                        sf = parse_serialized_file(data)
                    except UnityFormatError as exc:
                        event(LOG, logging.WARNING, "serialized_parse_failed", path=str(path), error=str(exc))
                        continue
                    serialized_files += 1
                    rel = path.relative_to(layout.root).as_posix()
                    for info in sf.objects:
                        class_id = info.class_id(sf.types)
                        class_name = CLASS_IDS.get(class_id, str(class_id))
                        class_counts[class_name] += 1
                        object_total += 1
                        name = _read_name(sf, data, info, class_id)
                        record = ObjectRecord(rel, info.path_id, class_id, class_name, name, info.byte_size)
                        obj_out.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
                        if class_id == TEXT_ASSET:
                            text_written += _extract_text_asset(sf, data, info, name, text_dir, used_names)
                        elif class_id == MONO_SCRIPT:
                            script = _read_mono_script(sf, data, info)
                            if script is not None:
                                mono_out.write(f"{rel}\t{info.path_id}\t{script.name}\t{script.class_name}\t"
                                               f"{script.namespace}\t{script.assembly}\n")
                                mono_total += 1

        result = AssetsResult(
            root=str(layout.root), serialized_files=serialized_files, skipped_bundles=skipped_bundles,
            objects=object_total, class_counts=dict(class_counts.most_common()),
            text_assets_written=text_written, mono_scripts=mono_total)
        (staging / "assets_report.json").write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False),
                                                     encoding="utf-8")
    event(LOG, logging.INFO, "assets_done", objects=object_total, text_assets=text_written, mono_scripts=mono_total)
    return result


def _extract_text_asset(sf: SerializedFile, data: bytes, info, name: str, text_dir: Path, used: set[str]) -> int:
    try:
        reader = ObjectReader(sf, data, info)
        reader.string()  # m_Name
        payload = reader.byte_array()  # m_Script
    except UnityFormatError as exc:
        event(LOG, logging.WARNING, "textasset_unreadable", path_id=info.path_id, error=str(exc))
        return 0
    _unique_path(text_dir, name or f"TextAsset_{info.path_id}", info.path_id, used).write_bytes(payload)
    return 1


def summarize_assets(result: AssetsResult) -> list[str]:
    lines = [
        f"root              : {result.root}",
        f"serialized files  : {result.serialized_files} ({result.skipped_bundles} bundles not unpacked)",
        f"objects           : {result.objects}",
        f"text assets        : {result.text_assets_written} written to textassets/",
        f"mono scripts      : {result.mono_scripts} in monoscripts.tsv",
    ]
    top = list(result.class_counts.items())[:12]
    lines.append("top classes       : " + ", ".join(f"{name} {count}" for name, count in top))
    return lines
