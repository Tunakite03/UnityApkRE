"""Merge an APK set (base + config/asset-pack splits + OBBs) into one extracted tree.

Layout of the output directory:
    Data/                 assets/bin/Data with `.splitN` parts concatenated
    lib/<abi>/            native libraries (libil2cpp.so, libunity.so, ...)
    assets/               every other entry under assets/ (Addressables, StreamingAssets)
    archives/<stem>/      remaining entries per archive (AndroidManifest.xml, dex, res, META-INF)
    extraction.json       what was read and written
"""

import json
import logging
import re
import shutil
import zipfile
import zlib
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence

from uare.errors import ApkFormatError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.apk")

UNITY_DATA_PREFIX = "assets/bin/Data/"
MAX_TOTAL_UNCOMPRESSED = 64 << 30  # refuse archive sets declaring more than 64 GiB of content
_SPLIT_PART_RE = re.compile(r"^(?P<logical>.+)\.split(?P<index>\d+)$")
_STEM_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_ABIS = frozenset({"arm64_v8a", "armeabi_v7a", "armeabi", "x86", "x86_64"})
_DENSITIES = frozenset({"ldpi", "mdpi", "tvdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi", "nodpi", "anydpi"})
# Errors zipfile raises while decompressing a damaged, encrypted, or unsupported entry.
_ZIP_READ_ERRORS = (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError)


@dataclass(frozen=True)
class ArchiveRole:
    kind: str  # base | config.abi | config.density | config.locale | split | obb | standalone
    detail: str | None


@dataclass(frozen=True)
class SourceEntry:
    archive: Path
    info: zipfile.ZipInfo


@dataclass(frozen=True)
class ArchiveSummary:
    path: str
    role: ArchiveRole
    entries: int


@dataclass(frozen=True)
class ExtractionResult:
    out_dir: str
    archives: tuple[ArchiveSummary, ...]
    files_written: int
    bytes_written: int
    merged_split_files: dict[str, int]
    duplicate_entries_skipped: int
    abis: tuple[str, ...]


@dataclass
class _PlanBuilder:
    """Accumulates destination -> source mappings while archives are scanned (local to one extraction)."""

    whole: dict[PurePosixPath, SourceEntry] = field(default_factory=dict)
    parts: dict[PurePosixPath, dict[int, SourceEntry]] = field(default_factory=dict)
    folded: dict[str, PurePosixPath] = field(default_factory=dict)
    duplicates: int = 0

    def _claim(self, dest: PurePosixPath) -> None:
        key = dest.as_posix().casefold()
        owner = self.folded.setdefault(key, dest)
        if owner != dest:
            raise ApkFormatError(f"{dest} and {owner} differ only by case and would collide on disk")

    def add(self, dest: PurePosixPath, source: SourceEntry) -> None:
        split = _SPLIT_PART_RE.match(dest.name) if dest.parts[0] == "Data" else None
        if split:
            target = dest.with_name(split.group("logical"))
            slots = self.parts.setdefault(target, {})
            index = int(split.group("index"))
            existing = slots.get(index)
            if existing is None:
                self._claim(target)
                slots[index] = source
                return
        else:
            existing = self.whole.get(dest)
            if existing is None:
                self._claim(dest)
                self.whole[dest] = source
                return
        if (existing.info.CRC, existing.info.file_size) != (source.info.CRC, source.info.file_size):
            raise ApkFormatError(f"conflicting entry {dest} in {existing.archive.name} and {source.archive.name}")
        self.duplicates += 1

    def validate(self) -> None:
        for logical, slots in self.parts.items():
            expected = set(range(len(slots)))
            if set(slots) != expected:
                mismatched = sorted(expected.symmetric_difference(slots))
                raise ApkFormatError(f"{logical} has non-contiguous .split parts; mismatched indices {mismatched}")
            if logical in self.whole:
                raise ApkFormatError(f"{logical} exists both as a whole file and as .split parts")
        files = set(self.whole) | set(self.parts)
        for path in files:
            for parent in path.parents:
                if parent in files:
                    raise ApkFormatError(f"{parent} is both a file and the directory of {path}")
        total = sum(s.info.file_size for s in self.whole.values())
        total += sum(s.info.file_size for slots in self.parts.values() for s in slots.values())
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise ApkFormatError(f"archives declare {total} uncompressed bytes, above the {MAX_TOTAL_UNCOMPRESSED} cap")


def classify_archive(path: Path) -> ArchiveRole:
    name = path.name.lower()
    if name.endswith(".obb"):
        return ArchiveRole(kind="obb", detail=name.split(".", 1)[0])
    if not name.endswith(".apk"):
        raise ApkFormatError(f"{path} is neither an .apk nor an .obb")
    if name == "base.apk":
        return ArchiveRole(kind="base", detail=None)
    config = re.fullmatch(r"split_config\.(.+)\.apk", name)
    if config:
        qualifier = config.group(1)
        if qualifier in _ABIS:
            return ArchiveRole(kind="config.abi", detail=qualifier.replace("_", "-"))
        if qualifier in _DENSITIES:
            return ArchiveRole(kind="config.density", detail=qualifier)
        return ArchiveRole(kind="config.locale", detail=qualifier)
    if name.startswith("split_"):
        return ArchiveRole(kind="split", detail=name[len("split_"):-len(".apk")])
    return ArchiveRole(kind="standalone", detail=None)


def safe_entry_path(name: str) -> PurePosixPath:
    if "\\" in name or ":" in name or name.startswith("/") or "\x00" in name:
        raise ApkFormatError(f"unsafe zip entry name: {name!r}")
    path = PurePosixPath(name)
    if not path.parts or ".." in path.parts:
        raise ApkFormatError(f"unsafe zip entry name: {name!r}")
    return path


def destination_for(entry: PurePosixPath, archive_stem: str) -> PurePosixPath:
    text = entry.as_posix()
    if text.startswith(UNITY_DATA_PREFIX):
        return PurePosixPath("Data") / text[len(UNITY_DATA_PREFIX):]
    if entry.parts[0] == "lib" and len(entry.parts) >= 3:
        return entry
    if entry.parts[0] == "assets":
        return entry
    return PurePosixPath("archives") / archive_stem / entry


def _copy(zf: zipfile.ZipFile, source: SourceEntry, dst: BinaryIO) -> int:
    try:
        with zf.open(source.info) as src:
            written = 0
            for chunk in iter(lambda: src.read(1 << 20), b""):
                dst.write(chunk)
                written += len(chunk)
    except _ZIP_READ_ERRORS as exc:
        raise ApkFormatError(f"cannot read {source.info.filename} from {source.archive.name}: {exc}") from exc
    if written != source.info.file_size:
        raise ApkFormatError(f"{source.info.filename} in {source.archive.name} produced {written} bytes, "
                             f"header declares {source.info.file_size}")
    return written


def _validate_inputs(archives: Sequence[Path]) -> None:
    if not archives:
        raise ApkFormatError("no archives given")
    for archive in archives:
        if not archive.is_file():
            raise ApkFormatError(f"{archive} does not exist or is not a file")
        if not _STEM_RE.fullmatch(archive.stem):
            raise ApkFormatError(f"archive name {archive.name!r} is not a plain file name")
    stems = [a.stem for a in archives]
    if len(set(stems)) != len(stems):
        raise ApkFormatError(f"archive names must be unique, got {stems}")


def extract_apk_set(archives: Sequence[Path], out_dir: Path) -> ExtractionResult:
    _validate_inputs(archives)
    plan = _PlanBuilder()
    summaries: list[ArchiveSummary] = []
    with ExitStack() as stack:
        opened: dict[Path, zipfile.ZipFile] = {}
        for archive in archives:
            role = classify_archive(archive)
            try:
                zf = stack.enter_context(zipfile.ZipFile(archive))
            except zipfile.BadZipFile as exc:
                raise ApkFormatError(f"{archive} is not a valid zip archive: {exc}") from exc
            opened[archive] = zf
            infos = [i for i in zf.infolist() if not i.is_dir()]
            for info in infos:
                plan.add(destination_for(safe_entry_path(info.filename), archive.stem), SourceEntry(archive, info))
            summaries.append(ArchiveSummary(path=str(archive), role=role, entries=len(infos)))
            event(LOG, logging.INFO, "archive_scanned", archive=archive.name, kind=role.kind, entries=len(infos))
        plan.validate()

        with staged_output(out_dir) as staging:
            free = shutil.disk_usage(staging).free
            declared = sum(s.info.file_size for s in plan.whole.values())
            declared += sum(s.info.file_size for slots in plan.parts.values() for s in slots.values())
            if declared > free:
                raise ApkFormatError(f"extraction needs {declared} bytes but only {free} are free at {out_dir.parent}")
            written_files = written_bytes = 0
            for dest, source in plan.whole.items():
                target = staging / dest
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as dst:
                    written_bytes += _copy(opened[source.archive], source, dst)
                written_files += 1
            for logical, slots in plan.parts.items():
                target = staging / logical
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as dst:
                    for index in range(len(slots)):
                        written_bytes += _copy(opened[slots[index].archive], slots[index], dst)
                written_files += 1

            abis = tuple(sorted({d.parts[1] for d in plan.whole if d.parts[0] == "lib"}))
            result = ExtractionResult(
                out_dir=str(out_dir),
                archives=tuple(summaries),
                files_written=written_files,
                bytes_written=written_bytes,
                merged_split_files={str(k): len(v) for k, v in sorted(plan.parts.items())},
                duplicate_entries_skipped=plan.duplicates,
                abis=abis,
            )
            (staging / "extraction.json").write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    event(LOG, logging.INFO, "extracted", files=written_files, bytes=written_bytes, merged=len(plan.parts),
          abis=list(abis))
    return result
