"""Categorised name tables from global-metadata.dat definition sections (metadata v29-31).

The flat string heap (see il2cpp.py) mixes every identifier together. This module reads the
definition tables and separates them into types, methods, fields, parameters, properties and
events, each with the name taken from the record's nameIndex. Type records also carry a
namespace.

What this module does NOT do, on purpose:

- It does not group members under their declaring type. The per-type method/field ranges live
  in Il2CppTypeDefinition, and in obfuscated builds (name indices and range fields scrambled)
  they are not recoverable without the binary and the game's own de-obfuscation. Grouping,
  signatures and RVAs are the job of Il2CppDumper / Il2CppInspector, run on an UNPACKED binary.
- It does not decrypt or de-obfuscate. Records whose nameIndex is out of range or not a clean
  identifier are emitted as "" and counted, never guessed.

The section layout is fixed for the metadata v29/30/31 family (31 section pairs). Every table is
validated at load (stride divides the section, nameIndex offset is a string-heap start for a
high fraction of records); a build that fails validation raises instead of emitting guesses.
"""

import json
import logging
import struct
from dataclasses import dataclass
from pathlib import Path

from uare.errors import Il2CppMetadataError
from uare.il2cpp import METADATA_MAGIC
from uare.logs import event

LOG = logging.getLogger("uare.il2cpp_dump")

SUPPORTED_VERSIONS = (29, 30, 31)
_SECTION_PAIRS = 31
_STRING_SECTION = 2

# (section index, record stride, nameIndex offset within the record) for the v29-31 header order.
# Verified by content against a real v31 metadata: each table's nameIndex resolves to a string
# heap start for the large majority of records (the rest are the build's obfuscated entries).
_TABLES: dict[str, tuple[int, int, int]] = {
    "events": (3, 24, 0),
    "properties": (4, 20, 0),
    "methods": (5, 36, 0),
    "parameters": (10, 12, 0),
    "fields": (11, 12, 0),
    "types": (19, 88, 0),
}
_TYPE_NAMESPACE_OFFSET = 4  # Il2CppTypeDefinition: nameIndex@0, namespaceIndex@4
_VALIDATION_SAMPLE = 4000


@dataclass(frozen=True)
class NameTable:
    name: str
    count: int
    unreadable: int  # records whose nameIndex was out of range or not a clean identifier
    path: str


@dataclass(frozen=True)
class DumpResult:
    version: int
    tables: tuple[NameTable, ...]
    note: str


def _read_pairs(data: bytes) -> list[tuple[int, int]]:
    return [struct.unpack_from("<II", data, 8 + i * 8) for i in range(_SECTION_PAIRS)]


def _string_at(heap: bytes, index: int) -> str | None:
    """Identifier starting at `index` in the string heap, or None if it is not a clean start."""
    if index < 0 or index >= len(heap):
        return None
    if index > 0 and heap[index - 1] != 0:
        return None
    end = heap.find(b"\x00", index)
    if end < 0:
        return None
    try:
        text = heap[index:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text.isprintable() else None


def _validate(data: bytes, heap: bytes, section: tuple[int, int], stride: int, name_off: int) -> float:
    offset, size = section
    count = size // stride
    if count == 0:
        return 0.0
    step = max(1, count // _VALIDATION_SAMPLE)
    checked = ok = 0
    for i in range(0, count, step):
        checked += 1
        idx = struct.unpack_from("<i", data, offset + i * stride + name_off)[0]
        ok += _string_at(heap, idx) is not None
    return ok / checked


def _emit_names(data: bytes, heap: bytes, section: tuple[int, int], stride: int, name_off: int,
                out_path: Path, with_namespace: bool) -> tuple[int, int]:
    offset, size = section
    count = size // stride
    unreadable = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for i in range(count):
            base = offset + i * stride
            name = _string_at(heap, struct.unpack_from("<i", data, base + name_off)[0])
            if name is None:
                unreadable += 1
                name = ""
            if with_namespace:
                namespace = _string_at(heap, struct.unpack_from("<i", data, base + _TYPE_NAMESPACE_OFFSET)[0]) or ""
                handle.write(f"{i}\t{namespace}\t{name}\n")
            else:
                handle.write(f"{i}\t{name}\n")
    return count, unreadable


def dump_definitions(metadata_path: Path, out_dir: Path) -> DumpResult:
    """Write one name table per definition section into out_dir. out_dir must already exist."""
    data = metadata_path.read_bytes()
    if len(data) < 8 + _SECTION_PAIRS * 8:
        raise Il2CppMetadataError(f"{metadata_path} is too small to be IL2CPP metadata")
    magic, version = struct.unpack_from("<Ii", data, 0)
    if magic != METADATA_MAGIC:
        raise Il2CppMetadataError(
            f"magic 0x{magic:08X} != 0x{METADATA_MAGIC:08X}: metadata is encrypted, custom, or not IL2CPP"
        )
    if version not in SUPPORTED_VERSIONS:
        raise Il2CppMetadataError(
            f"structured dump supports metadata v{SUPPORTED_VERSIONS}; this file is v{version}. "
            f"Flat identifier/literal extraction (uare analyze) still works."
        )
    pairs = _read_pairs(data)
    for name, (section_index, _stride, _off) in _TABLES.items():
        offset, size = pairs[section_index]
        if offset + size > len(data):
            raise Il2CppMetadataError(f"section for {name} [{offset}, +{size}) exceeds file size {len(data)}")
    heap_off, heap_size = pairs[_STRING_SECTION]
    heap = data[heap_off:heap_off + heap_size]

    tables: list[NameTable] = []
    total_unreadable = 0
    for name, (section_index, stride, name_off) in _TABLES.items():
        section = pairs[section_index]
        if section[1] % stride:
            raise Il2CppMetadataError(f"{name} section size {section[1]} is not a multiple of stride {stride}")
        validity = _validate(data, heap, section, stride, name_off)
        if validity < 0.25:
            raise Il2CppMetadataError(
                f"{name} table failed validation (only {validity:.0%} of names resolve); "
                f"section layout does not match metadata v{version} as expected"
            )
        out_path = out_dir / f"{name}.tsv"
        count, unreadable = _emit_names(data, heap, section, stride, name_off, out_path, name == "types")
        total_unreadable += unreadable
        tables.append(NameTable(name=name, count=count, unreadable=unreadable, path=out_path.name))
        event(LOG, logging.INFO, "il2cpp_table", table=name, count=count, unreadable=unreadable,
              validity=round(validity, 3))

    note = (
        "Flat name tables only. Member-to-type grouping, signatures and RVAs require "
        "Il2CppDumper/Il2CppInspector on an unpacked binary."
    )
    if total_unreadable:
        note += (f" {total_unreadable} names were unreadable (obfuscated or blob entries) and emitted as empty; "
                 f"this build scrambles some name indices.")
    (out_dir / "sections.json").write_text(
        json.dumps({"version": version, "sections": [{"index": i, "offset": o, "size": s}
                                                      for i, (o, s) in enumerate(pairs)]}, indent=2),
        encoding="utf-8")
    return DumpResult(version=version, tables=tuple(tables), note=note)
