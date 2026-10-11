"""Categorised name tables and member grouping from global-metadata.dat.

The flat string heap (see il2cpp.py) mixes every identifier together. This module reads the
definition tables and separates them into types, methods, fields, parameters, properties and
events, each with the name taken from the record's nameIndex. Type records also carry a
namespace.

Member grouping (members.tsv): the per-type member ranges (methodStart/method_count, fieldStart/
field_count, eventStart/event_count, propertyStart/property_count) live in Il2CppTypeDefinition
and index into the method/field/event/property tables, all of which are in the metadata. So
which method/field belongs to which class IS recoverable from the metadata alone, with no binary
and no unpacking. This module emits that grouping.

Supported metadata versions: 24 (24.0/24.1 and 24.2/24.4 layouts), 27, 28, 29, 30 and 31. The
section indices of the type/method/field/property/event/parameter/string tables are the same for
all of them (the header keeps those fields in a fixed order; only optional fields that come after
typeDefinitions vary). What differs and is selected here per version is the record stride and the
Il2CppTypeDefinition member offsets, taken from Unity's metadata layout.

What this module still does NOT do, on purpose:

- It does not resolve SIGNATURES (parameter/return type names) or RVAs. Those need the
  Il2CppType[] array, which lives in libil2cpp.so (Il2CppMetadataRegistration.types), not in the
  metadata; that is the job of Il2CppDumper / Il2CppInspector on an unpacked binary.
- It does not resolve base-type, nested-type or interface names: their indices also point into
  the binary Il2CppType[] array, not into the metadata type-definition table.
- It does not decrypt or de-obfuscate. Records whose nameIndex is out of range or not a clean
  identifier are emitted as "" and counted, never guessed. A type whose member range does not fit
  its table is skipped (and counted) rather than emitting a wrong grouping.

Every table is validated at load (stride divides the section, nameIndex offset is a string-heap
start for a high fraction of records); a build that fails validation raises instead of emitting
guesses, so a wrong version guess can never produce silent garbage.
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

SUPPORTED_VERSIONS = (24, 27, 28, 29, 30, 31)
_SECTION_PAIRS = 31  # enough for every supported version: we only read up to index 19 (v24 has 33)
_STRING_SECTION = 2
_EVENTS_SECTION, _PROPERTIES_SECTION, _METHODS_SECTION = 3, 4, 5
_PARAMETERS_SECTION, _FIELDS_SECTION, _TYPES_SECTION = 10, 11, 19
_IMAGES_SECTION = 20
# Il2CppImageDefinition: nameIndex, assemblyIndex, typeStart, typeCount, ... ; v24.1+ appends
# customAttributeStart/Count (40 bytes), v24.0 does not (32 bytes).
_IMAGE_STRIDES = (40, 32)
_TYPE_NAMESPACE_OFFSET = 4  # Il2CppTypeDefinition: nameIndex@0, namespaceIndex@4
_VALIDATION_SAMPLE = 4000
_MAX_IDENTIFIER = 1024  # a managed identifier longer than this is treated as not-a-name
_V24_32_PAIR_HEADER = 8 + 32 * 8  # v24.2/24.4 header is 32 section pairs; v24.0/24.1 is 33


@dataclass(frozen=True)
class Layout:
    """Per-version record strides and Il2CppTypeDefinition member offsets."""
    name: str
    versions: tuple[int, ...]
    method_stride: int
    field_stride: int
    property_stride: int
    event_stride: int
    parameter_stride: int
    type_stride: int
    type_field_start: int
    type_method_start: int
    type_event_start: int
    type_property_start: int
    type_method_count: int
    type_property_count: int
    type_field_count: int
    type_event_count: int


# Il2CppTypeDefinition omits byrefTypeIndex (and earlier rgctx/custom-attribute fields) from v27
# on, so the member offsets sit 4 bytes earlier than the <=24.5 layout. Derived from Unity's
# Il2CppTypeDefinition / Il2CppMethodDefinition field order (see Il2CppDumper's MetadataClass.cs).
_V24 = Layout(
    name="v24", versions=(24,), method_stride=56, field_stride=16, property_stride=24, event_stride=28,
    parameter_stride=16, type_stride=104, type_field_start=48, type_method_start=52,
    type_event_start=56, type_property_start=60, type_method_count=80, type_property_count=82,
    type_field_count=84, type_event_count=86)
_V24_2 = Layout(
    name="v24.2", versions=(24,), method_stride=36, field_stride=12, property_stride=20, event_stride=24,
    parameter_stride=12, type_stride=96, type_field_start=40, type_method_start=44,
    type_event_start=48, type_property_start=52, type_method_count=72, type_property_count=74,
    type_field_count=76, type_event_count=78)
_V27 = Layout(
    name="v27", versions=(27, 28, 29, 30), method_stride=32, field_stride=12, property_stride=20,
    event_stride=24, parameter_stride=12, type_stride=88, type_field_start=32, type_method_start=36,
    type_event_start=40, type_property_start=44, type_method_count=64, type_property_count=66,
    type_field_count=68, type_event_count=70)
_V31 = Layout(
    name="v31", versions=(31,), method_stride=36, field_stride=12, property_stride=20, event_stride=24,
    parameter_stride=12, type_stride=88, type_field_start=32, type_method_start=36, type_event_start=40,
    type_property_start=44, type_method_count=64, type_property_count=66, type_field_count=68,
    type_event_count=70)


@dataclass(frozen=True)
class NameTable:
    name: str
    count: int
    unreadable: int  # records whose nameIndex was out of range or not a clean identifier
    path: str


@dataclass(frozen=True)
class GroupingResult:
    types_total: int
    types_grouped: int  # types with at least one valid member range
    member_rows: int  # rows written to members.tsv
    ranges_skipped: int  # member ranges that did not fit their table (obfuscated or mismatched)
    valid_fraction: float  # fraction of types whose member ranges all fit


@dataclass(frozen=True)
class DumpResult:
    version: int
    layout: str
    tables: tuple[NameTable, ...]
    grouping: GroupingResult
    note: str


def detect_layout(version: int, first_section_offset: int) -> Layout:
    if version == 31:
        return _V31
    if version in (27, 28, 29, 30):
        return _V27
    if version == 24:
        # 24.2/24.4 dropped the rgctxEntries pair, making the header 32 pairs (offset 264).
        return _V24_2 if first_section_offset == _V24_32_PAIR_HEADER else _V24
    raise Il2CppMetadataError(
        f"structured dump supports metadata v{SUPPORTED_VERSIONS}; this file is v{version}. "
        f"Flat identifier/literal extraction (uare analyze) still works."
    )


def _tables(layout: Layout) -> dict[str, tuple[int, int, int]]:
    """(section index, record stride, nameIndex offset) for each emitted name table."""
    return {
        "events": (_EVENTS_SECTION, layout.event_stride, 0),
        "properties": (_PROPERTIES_SECTION, layout.property_stride, 0),
        "methods": (_METHODS_SECTION, layout.method_stride, 0),
        "parameters": (_PARAMETERS_SECTION, layout.parameter_stride, 0),
        "fields": (_FIELDS_SECTION, layout.field_stride, 0),
        "types": (_TYPES_SECTION, layout.type_stride, 0),
    }


def _member_groups(layout: Layout) -> dict[str, tuple[int, int, int, int]]:
    """kind -> (section index, member stride, type-def start offset, type-def count offset)."""
    return {
        "method": (_METHODS_SECTION, layout.method_stride, layout.type_method_start, layout.type_method_count),
        "field": (_FIELDS_SECTION, layout.field_stride, layout.type_field_start, layout.type_field_count),
        "property": (_PROPERTIES_SECTION, layout.property_stride, layout.type_property_start, layout.type_property_count),
        "event": (_EVENTS_SECTION, layout.event_stride, layout.type_event_start, layout.type_event_count),
    }


def _read_pairs(data: bytes) -> list[tuple[int, int]]:
    return [struct.unpack_from("<II", data, 8 + i * 8) for i in range(_SECTION_PAIRS)]


def _string_at(heap: bytes, index: int) -> str | None:
    """Identifier starting at `index` in the string heap, or None if it is not a clean start."""
    if index < 0 or index >= len(heap):
        return None
    if index > 0 and heap[index - 1] != 0:
        return None
    end = heap.find(b"\x00", index, index + _MAX_IDENTIFIER + 1)
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
        return 1.0  # an empty table is vacuously valid
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


def _member_names(data: bytes, heap: bytes, section: tuple[int, int], stride: int) -> list[str]:
    offset, size = section
    count = size // stride
    names: list[str] = []
    for i in range(count):
        names.append(_string_at(heap, struct.unpack_from("<i", data, offset + i * stride)[0]) or "")
    return names


def _group_members(data: bytes, heap: bytes, pairs: list[tuple[int, int]], layout: Layout,
                   out_path: Path) -> GroupingResult:
    """Group methods/fields/events/properties under their declaring type using the ranges stored
    in Il2CppTypeDefinition. Rows whose range does not fit the member table are skipped."""
    groups = _member_groups(layout)
    types_off, types_size = pairs[_TYPES_SECTION]
    types_total = types_size // layout.type_stride
    names = {kind: _member_names(data, heap, pairs[section], stride)
             for kind, (section, stride, _start, _count) in groups.items()}
    rows = grouped = skipped = valid_types = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for i in range(types_total):
            base = types_off + i * layout.type_stride
            type_name = _string_at(heap, struct.unpack_from("<i", data, base)[0]) or ""
            type_ok = True
            members: list[tuple[str, int, str]] = []
            for kind in ("method", "field", "event", "property"):
                _section, _stride, start_off, count_off = groups[kind]
                start = struct.unpack_from("<i", data, base + start_off)[0]
                count = struct.unpack_from("<H", data, base + count_off)[0]
                if count == 0:
                    continue
                table = names[kind]
                if start < 0 or start + count > len(table):
                    type_ok = False
                    skipped += 1
                    continue
                members.extend((kind, start + j, table[start + j]) for j in range(count))
            if members:
                grouped += 1
                valid_types += type_ok
                for kind, index, member_name in members:
                    handle.write(f"{i}\t{type_name}\t{kind}\t{index}\t{member_name}\n")
                    rows += 1
            elif type_ok:
                valid_types += 1
    fraction = valid_types / types_total if types_total else 1.0
    return GroupingResult(types_total=types_total, types_grouped=grouped, member_rows=rows,
                          ranges_skipped=skipped, valid_fraction=fraction)


def _emit_images(data: bytes, heap: bytes, pairs: list[tuple[int, int]], types_total: int,
                 out_path: Path) -> int | None:
    """Write images.tsv (index, assembly, type_start, type_count). Returns the image count, or None
    when no stride yields image ranges that tile the type table exactly (layout not recognised)."""
    offset, size = pairs[_IMAGES_SECTION]
    if offset + size > len(data):
        return None
    for stride in _IMAGE_STRIDES:
        if size == 0 or size % stride:
            continue
        records = []
        for i in range(size // stride):
            name_idx, _assembly, start, count = struct.unpack_from("<iiii", data, offset + i * stride)
            records.append((_string_at(heap, name_idx), start, count))
        expected = 0
        tiles = True
        for name, start, count in records:
            if name is None or start != expected or count < 0:
                tiles = False
                break
            expected += count
        if not tiles or expected != types_total:
            continue
        with out_path.open("w", encoding="utf-8") as handle:
            for i, (name, start, count) in enumerate(records):
                handle.write(f"{i}\t{name}\t{start}\t{count}\n")
        return len(records)
    return None


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
    layout = detect_layout(version, struct.unpack_from("<I", data, 8)[0])
    pairs = _read_pairs(data)
    tables_by_name = _tables(layout)
    for name, (section_index, _stride, _off) in tables_by_name.items():
        offset, size = pairs[section_index]
        if offset + size > len(data):
            raise Il2CppMetadataError(f"section for {name} [{offset}, +{size}) exceeds file size {len(data)}")
    heap_off, heap_size = pairs[_STRING_SECTION]
    heap = data[heap_off:heap_off + heap_size]

    tables: list[NameTable] = []
    total_unreadable = 0
    for name, (section_index, stride, name_off) in tables_by_name.items():
        section = pairs[section_index]
        if section[1] % stride:
            raise Il2CppMetadataError(
                f"{name} section size {section[1]} is not a multiple of stride {stride} for {layout.name}")
        validity = _validate(data, heap, section, stride, name_off)
        if validity < 0.25:
            raise Il2CppMetadataError(
                f"{name} table failed validation (only {validity:.0%} of names resolve); "
                f"the layout for metadata v{version} ({layout.name}) does not match this file"
            )
        out_path = out_dir / f"{name}.tsv"
        count, unreadable = _emit_names(data, heap, section, stride, name_off, out_path, name == "types")
        total_unreadable += unreadable
        tables.append(NameTable(name=name, count=count, unreadable=unreadable, path=out_path.name))
        event(LOG, logging.INFO, "il2cpp_table", table=name, count=count, unreadable=unreadable,
              validity=round(validity, 3))

    grouping = _group_members(data, heap, pairs, layout, out_dir / "members.tsv")
    event(LOG, logging.INFO, "il2cpp_grouping", types=grouping.types_total, grouped=grouping.types_grouped,
          members=grouping.member_rows, skipped=grouping.ranges_skipped,
          valid_fraction=round(grouping.valid_fraction, 3))

    images = _emit_images(data, heap, pairs, grouping.types_total, out_dir / "images.tsv")
    if images is None:
        event(LOG, logging.WARNING, "il2cpp_images_unrecognised", version=version, layout=layout.name)
    else:
        event(LOG, logging.INFO, "il2cpp_images", images=images)

    note = (
        f"Member grouping is in members.tsv (type, kind, member), using the {layout.name} layout. "
        "Signatures, base/nested/interface names and RVAs need the Il2CppType[] array in libil2cpp.so, "
        "so they require Il2CppDumper/Il2CppInspector on an unpacked binary."
    )
    if images is None:
        note += " The image (assembly) table did not validate, so images.tsv was not written."
    if total_unreadable:
        note += (f" {total_unreadable} names were unreadable (obfuscated or blob entries) and emitted as empty; "
                 f"this build scrambles some name indices.")
    if grouping.ranges_skipped:
        note += (f" {grouping.ranges_skipped} member ranges did not fit their table and were skipped "
                 f"({grouping.valid_fraction:.0%} of types grouped cleanly).")
    (out_dir / "sections.json").write_text(
        json.dumps({"version": version, "layout": layout.name,
                    "sections": [{"index": i, "offset": o, "size": s} for i, (o, s) in enumerate(pairs)]}, indent=2),
        encoding="utf-8")
    return DumpResult(version=version, layout=layout.name, tables=tuple(tables), grouping=grouping, note=note)
