"""Static analysis of an extracted Unity Android build. Writes report.json plus detail files.

Accepted roots:
    * an `uare extract` output directory (contains Data/ and usually lib/)
    * a raw Unity `Data` directory (contains globalgamemanagers or data.unity3d)

Output directory contents:
    report.json                summary (inventory, scripting backend, IL2CPP, native libraries)
    files.jsonl                one record per file under Data/
    il2cpp/identifiers.tsv     offset<TAB>name from the metadata string heap
    il2cpp/string_literals.jsonl
"""

import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from uare.elf import ElfReport, inspect_elf
from uare.errors import ElfFormatError, Il2CppMetadataError, UareError, UnityFormatError
from uare.il2cpp import read_metadata
from uare.logs import event
from uare.outdir import staged_output
from uare.unity import FileKind, detect_kind, read_bundle_header, read_serialized_header

LOG = logging.getLogger("uare.analyze")

_GUID_NAME_RE = re.compile(r"^[0-9a-f]{32}$")
_DATA_MARKERS = ("globalgamemanagers", "data.unity3d", "mainData")
_METADATA_REL = Path("Managed/Metadata/global-metadata.dat")


@dataclass(frozen=True)
class Layout:
    root: Path
    data_dir: Path
    lib_dir: Path | None
    assets_dir: Path | None  # assets/ outside bin/Data: Addressables (aa/), StreamingAssets, asset packs


@dataclass(frozen=True)
class FileRecord:
    path: str
    kind: str
    size: int
    format_version: int | None
    unity_version: str | None
    type_count: int | None
    target_platform: str | None
    type_tree: bool | None
    compression: str | None
    error: str | None


@dataclass(frozen=True)
class Inventory:
    total_files: int
    total_bytes: int
    kinds: dict[str, int]
    unity_versions: dict[str, int]
    target_platforms: dict[str, int]
    type_tree_files: int
    guid_named_files: int
    scene_files: tuple[str, ...]
    bundle_compression: dict[str, int]
    parse_errors: int


@dataclass(frozen=True)
class Il2CppSummary:
    status: str  # ok | absent | error
    detail: str | None
    version: int | None
    identifiers: int | None
    non_text_heap_entries: int | None
    string_literals: int | None
    non_utf8_literals: int | None


@dataclass(frozen=True)
class NativeLibrary:
    abi: str
    name: str
    report: ElfReport | None
    error: str | None


@dataclass(frozen=True)
class AnalysisReport:
    root: str
    data_dir: str
    scripting_backend: str
    scripting_assemblies: tuple[str, ...]
    boot_config: dict[str, str]
    inventory: Inventory
    assets_inventory: Inventory | None
    il2cpp: Il2CppSummary
    native_libraries: tuple[NativeLibrary, ...]


def resolve_layout(root: Path) -> Layout:
    if (root / "Data").is_dir():
        lib, assets = root / "lib", root / "assets"
        return Layout(root=root, data_dir=root / "Data", lib_dir=lib if lib.is_dir() else None,
                      assets_dir=assets if assets.is_dir() else None)
    if any((root / marker).exists() for marker in _DATA_MARKERS):
        return Layout(root=root, data_dir=root, lib_dir=None, assets_dir=None)
    native = sorted({p.name for p in root.glob("lib/*/*.so")})
    hint = f"; native libraries present: {native}" if native else ""
    raise UareError(f"{root} has no Unity player data (no Data/ or globalgamemanagers); "
                    f"not a Unity build or not an extraction directory{hint}")


def _record(path: Path, data_dir: Path) -> FileRecord:
    rel = path.relative_to(data_dir).as_posix()
    size = path.stat().st_size
    kind = detect_kind(path)
    try:
        if kind is FileKind.SERIALIZED:
            header = read_serialized_header(path)
            return FileRecord(rel, kind.value, size, header.format_version, header.unity_version,
                              header.type_count, header.target_platform_name, header.type_tree_enabled, None, None)
        if kind in (FileKind.BUNDLE_UNITYFS, FileKind.BUNDLE_LEGACY):
            bundle = read_bundle_header(path)
            return FileRecord(rel, kind.value, size, bundle.format_version, bundle.engine_version,
                              None, None, None, bundle.compression, None)
    except UnityFormatError as exc:
        event(LOG, logging.WARNING, "header_parse_failed", path=rel, error=str(exc))
        return FileRecord(rel, kind.value, size, None, None, None, None, None, None, str(exc))
    return FileRecord(rel, kind.value, size, None, None, None, None, None, None, None)


def build_inventory(data_dir: Path) -> tuple[Inventory, list[FileRecord]]:
    records = [_record(p, data_dir) for p in sorted(data_dir.rglob("*")) if p.is_file()]
    kinds = Counter(r.kind for r in records)
    versions = Counter(r.unity_version for r in records if r.unity_version)
    compression = Counter(r.compression for r in records if r.compression)
    platforms = Counter(r.target_platform for r in records if r.target_platform)
    type_tree = sum(1 for r in records if r.type_tree)
    scenes = tuple(r.path for r in records if re.fullmatch(r"level\d+", r.path))
    inventory = Inventory(
        total_files=len(records),
        total_bytes=sum(r.size for r in records),
        kinds=dict(kinds.most_common()),
        unity_versions=dict(versions.most_common()),
        target_platforms=dict(platforms.most_common()),
        type_tree_files=type_tree,
        guid_named_files=sum(1 for r in records if _GUID_NAME_RE.fullmatch(Path(r.path).name)),
        scene_files=tuple(sorted(scenes, key=lambda s: int(s[5:]))),
        bundle_compression=dict(compression.most_common()),
        parse_errors=sum(1 for r in records if r.error),
    )
    return inventory, records


def read_boot_config(data_dir: Path) -> dict[str, str]:
    path = data_dir / "boot.config"
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UnityFormatError(f"{path} is not UTF-8 text: {exc}") from exc
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
    return pairs


def read_scripting_assemblies(data_dir: Path) -> tuple[str, ...]:
    path = data_dir / "ScriptingAssemblies.json"
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnityFormatError(f"{path} is not valid UTF-8 JSON: {exc}") from exc
    names = payload.get("names") if isinstance(payload, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise UnityFormatError(f"{path} has no string list under 'names'")
    return tuple(names)


def detect_backend(layout: Layout) -> str:
    has_metadata = (layout.data_dir / _METADATA_REL).is_file()
    has_libil2cpp = layout.lib_dir is not None and any(layout.lib_dir.glob("*/libil2cpp.so"))
    has_managed_dlls = any((layout.data_dir / "Managed").glob("*.dll"))
    if has_metadata or has_libil2cpp:
        return "il2cpp"
    if has_managed_dlls:
        return "mono"
    return "unknown"


def _write_il2cpp(data_dir: Path, out_dir: Path) -> Il2CppSummary:
    path = data_dir / _METADATA_REL
    if not path.is_file():
        return Il2CppSummary("absent", None, None, None, None, None, None)
    try:
        header, identifiers, literals = read_metadata(path)
    except Il2CppMetadataError as exc:
        event(LOG, logging.WARNING, "il2cpp_metadata_unreadable", path=str(path), error=str(exc))
        return Il2CppSummary("error", str(exc), None, None, None, None, None)
    target = out_dir / "il2cpp"
    target.mkdir()
    with (target / "identifiers.tsv").open("w", encoding="utf-8") as handle:
        for offset, text in identifiers.entries:
            handle.write(f"{offset}\t{text}\n")
    with (target / "string_literals.jsonl").open("w", encoding="utf-8") as handle:
        for index, text in enumerate(literals.literals):
            handle.write(json.dumps({"index": index, "value": text}, ensure_ascii=False) + "\n")
    return Il2CppSummary("ok", None, header.version, len(identifiers.entries), identifiers.non_text_entries,
                         len(literals.literals), literals.non_utf8_literals)


def _native_libraries(lib_dir: Path | None) -> tuple[NativeLibrary, ...]:
    if lib_dir is None:
        return ()
    results: list[NativeLibrary] = []
    for path in sorted(lib_dir.glob("*/*.so")):
        try:
            results.append(NativeLibrary(path.parent.name, path.name, inspect_elf(path), None))
        except ElfFormatError as exc:
            event(LOG, logging.WARNING, "elf_unreadable", path=str(path), error=str(exc))
            results.append(NativeLibrary(path.parent.name, path.name, None, str(exc)))
    return tuple(results)


def _write_records(path: Path, records: list[FileRecord]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def analyze(root: Path, out_dir: Path) -> AnalysisReport:
    layout = resolve_layout(root)
    with staged_output(out_dir) as staging:
        event(LOG, logging.INFO, "analyze_start", data_dir=str(layout.data_dir), lib_dir=str(layout.lib_dir))
        inventory, records = build_inventory(layout.data_dir)
        _write_records(staging / "files.jsonl", records)
        assets_inventory = None
        if layout.assets_dir is not None:
            assets_inventory, asset_records = build_inventory(layout.assets_dir)
            _write_records(staging / "assets_files.jsonl", asset_records)

        report = AnalysisReport(
            root=str(layout.root),
            data_dir=str(layout.data_dir),
            scripting_backend=detect_backend(layout),
            scripting_assemblies=read_scripting_assemblies(layout.data_dir),
            boot_config=read_boot_config(layout.data_dir),
            inventory=inventory,
            assets_inventory=assets_inventory,
            il2cpp=_write_il2cpp(layout.data_dir, staging),
            native_libraries=_native_libraries(layout.lib_dir),
        )
        (staging / "report.json").write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False),
                                             encoding="utf-8")
    event(LOG, logging.INFO, "analyze_done", files=inventory.total_files, parse_errors=inventory.parse_errors,
          backend=report.scripting_backend, il2cpp=report.il2cpp.status)
    return report


def load_report(analysis_dir: Path) -> dict[str, Any]:
    path = analysis_dir / "report.json"
    if not path.is_file():
        raise UareError(f"{path} not found; run `uare analyze` first")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UareError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "inventory" not in payload:
        raise UareError(f"{path} is not a uare report")
    return payload


def summarize(report: dict[str, Any]) -> list[str]:
    """Human summary of a report.json payload (the asdict form of AnalysisReport)."""
    inv = report["inventory"]
    lines = [
        f"data dir          : {report['data_dir']}",
        f"scripting backend : {report['scripting_backend']}",
        f"unity versions    : {inv['unity_versions'] or '-'}",
        f"platforms         : {inv['target_platforms'] or '-'}",
        f"files             : {inv['total_files']} ({inv['total_bytes'] / 1e6:.1f} MB), kinds {inv['kinds']}",
        f"guid-named files  : {inv['guid_named_files']}",
        f"scenes            : {', '.join(inv['scene_files']) or '-'}",
        f"type trees        : {inv['type_tree_files']} serialized files carry type trees",
        f"bundles           : {inv['bundle_compression'] or '-'}",
        f"header errors     : {inv['parse_errors']} (see files.jsonl)",
        f"assemblies        : {len(report['scripting_assemblies'])}",
    ]
    assets = report.get("assets_inventory")
    if assets is not None:
        lines.append(f"assets/ (aa, SA)  : {assets['total_files']} files, kinds {assets['kinds']}, "
                     f"bundles {assets['bundle_compression'] or '-'}")
    il = report["il2cpp"]
    if il["status"] == "ok":
        lines.append(f"il2cpp metadata   : v{il['version']}, {il['identifiers']} identifiers, "
                     f"{il['string_literals']} literals")
    else:
        lines.append(f"il2cpp metadata   : {il['status']}{': ' + il['detail'] if il['detail'] else ''}")
    for lib in report["native_libraries"]:
        r = lib["report"]
        if r is None:
            lines.append(f"lib {lib['abi']}/{lib['name']}: unreadable ({lib['error']})")
            continue
        packer = f", packer {r['packer']}" if r.get("packer") else ""
        lines.append(
            f"lib {lib['abi']}/{lib['name']}: {r['size'] / 1e6:.1f} MB, exec entropy {r['exec_entropy']}, "
            f"{r['packing_verdict']}{packer}, il2cpp exports {len(r['symbols']['il2cpp_exports'])}, "
            f"section table {r['symbols']['section_table']}"
        )
    return lines
