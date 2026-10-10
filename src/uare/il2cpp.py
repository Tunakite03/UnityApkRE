"""IL2CPP global-metadata.dat: header validation plus the identifier and string-literal tables.

Only the first three header sections are interpreted; their order (stringLiteral,
stringLiteralData, string) is stable from metadata v16 through v31. Later sections move
between versions and are left to dedicated dumpers. A wrong magic is reported as a
finding (encrypted or custom metadata); this module does not attempt to decrypt it.
"""

import struct
from dataclasses import dataclass
from pathlib import Path

from uare.errors import Il2CppMetadataError

METADATA_MAGIC = 0xFAB11BAF
_MIN_VERSION, _MAX_VERSION = 16, 40
_SECTION_NAMES = ("stringLiteral", "stringLiteralData", "string")
_LITERAL_ENTRY = struct.Struct("<Ii")  # Il2CppStringLiteral { uint32 length; int32 dataIndex; }


@dataclass(frozen=True)
class Section:
    name: str
    offset: int
    size: int


@dataclass(frozen=True)
class MetadataHeader:
    version: int
    file_size: int
    sections: tuple[Section, ...]

    def section(self, name: str) -> Section:
        for section in self.sections:
            if section.name == name:
                return section
        raise Il2CppMetadataError(f"section {name!r} not parsed")


@dataclass(frozen=True)
class IdentifierTable:
    entries: tuple[tuple[int, str], ...]  # (offset within string section, text)
    non_text_entries: int


@dataclass(frozen=True)
class LiteralTable:
    literals: tuple[str, ...]
    non_utf8_literals: int


def parse_metadata_header(head: bytes, file_size: int) -> MetadataHeader:
    if len(head) < 8 + 8 * len(_SECTION_NAMES):
        raise Il2CppMetadataError(f"metadata header truncated ({len(head)} bytes)")
    magic, version = struct.unpack_from("<Ii", head, 0)
    if magic != METADATA_MAGIC:
        raise Il2CppMetadataError(
            f"magic 0x{magic:08X} != 0x{METADATA_MAGIC:08X}: metadata is encrypted, custom, or not IL2CPP"
        )
    if not _MIN_VERSION <= version <= _MAX_VERSION:
        raise Il2CppMetadataError(f"unsupported metadata version {version}")
    values = struct.unpack_from(f"<{2 * len(_SECTION_NAMES)}I", head, 8)
    sections = tuple(Section(name, values[2 * i], values[2 * i + 1]) for i, name in enumerate(_SECTION_NAMES))
    for section in sections:
        if section.offset + section.size > file_size:
            raise Il2CppMetadataError(
                f"section {section.name} [{section.offset}, +{section.size}) exceeds file size {file_size}"
            )
    return MetadataHeader(version=version, file_size=file_size, sections=sections)


def parse_identifier_table(section_bytes: bytes) -> IdentifierTable:
    """Split the string heap on NUL. Blobs stored in the heap (public keys) are counted, not emitted."""
    entries: list[tuple[int, str]] = []
    non_text = 0
    offset = 0
    for raw in section_bytes.split(b"\x00"):
        if raw:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and text.isprintable():
                entries.append((offset, text))
            else:
                non_text += 1
        offset += len(raw) + 1
    return IdentifierTable(entries=tuple(entries), non_text_entries=non_text)


def parse_literal_table(table: bytes, data: bytes) -> LiteralTable:
    if len(table) % _LITERAL_ENTRY.size:
        raise Il2CppMetadataError(f"stringLiteral table size {len(table)} is not a multiple of {_LITERAL_ENTRY.size}")
    entries = list(_LITERAL_ENTRY.iter_unpack(table))
    declared = sum(length for length, _ in entries)
    if declared > len(data):
        raise Il2CppMetadataError(
            f"string literals declare {declared} bytes but stringLiteralData holds {len(data)}; table is inconsistent"
        )
    literals: list[str] = []
    non_utf8 = 0
    for index, (length, data_index) in enumerate(entries):
        if data_index < 0 or data_index + length > len(data):
            raise Il2CppMetadataError(
                f"string literal {index} [{data_index}, +{length}) exceeds stringLiteralData size {len(data)}"
            )
        raw = data[data_index:data_index + length]
        try:
            literals.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            non_utf8 += 1
            literals.append(raw.decode("utf-8", errors="backslashreplace"))
    return LiteralTable(literals=tuple(literals), non_utf8_literals=non_utf8)


def _slice(path: Path, section: Section) -> bytes:
    with path.open("rb") as handle:
        handle.seek(section.offset)
        data = handle.read(section.size)
    if len(data) != section.size:
        raise Il2CppMetadataError(f"short read of section {section.name} in {path}")
    return data


def read_metadata(path: Path) -> tuple[MetadataHeader, IdentifierTable, LiteralTable]:
    if not path.is_file():
        raise Il2CppMetadataError(f"{path} does not exist")
    with path.open("rb") as handle:
        head = handle.read(256)
    header = parse_metadata_header(head, path.stat().st_size)
    identifiers = parse_identifier_table(_slice(path, header.section("string")))
    literals = parse_literal_table(
        _slice(path, header.section("stringLiteral")), _slice(path, header.section("stringLiteralData"))
    )
    return header, identifiers, literals
