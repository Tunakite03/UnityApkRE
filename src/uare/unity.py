"""Header-level parsers for Unity player data: SerializedFile (format 9+) and AssetBundles."""

import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from uare.errors import UnityFormatError

_HEAD_BYTES = 4096
_MIN_FORMAT, _MAX_FORMAT = 9, 50
_RESOURCE_SUFFIXES = frozenset({".ress", ".resource"})
_TEXT_SUFFIXES = frozenset({".json", ".config", ".txt", ".xml"})
_TEXT_NAMES = frozenset({"unity_app_guid"})
_LEGACY_BUNDLE_SIGNATURES = (b"UnityWeb\x00", b"UnityRaw\x00", b"UnityArchive\x00")

# BuildTarget values as stored in SerializedFile.m_TargetPlatform.
PLATFORM_NAMES: dict[int, str] = {
    2: "StandaloneOSX",
    5: "StandaloneWindows",
    9: "iOS",
    13: "Android",
    19: "StandaloneWindows64",
    20: "WebGL",
    24: "StandaloneLinux64",
    38: "Switch",
}

COMPRESSION_NAMES: dict[int, str] = {0: "none", 1: "lzma", 2: "lz4", 3: "lz4hc"}


class FileKind(str, Enum):
    SERIALIZED = "serialized"
    BUNDLE_UNITYFS = "bundle_unityfs"
    BUNDLE_LEGACY = "bundle_legacy"
    RESOURCE = "resource"
    TEXT = "text"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SerializedFileHeader:
    format_version: int
    metadata_size: int
    file_size: int
    data_offset: int
    big_endian: bool
    unity_version: str
    target_platform: int
    target_platform_name: str
    type_tree_enabled: bool
    type_count: int


@dataclass(frozen=True)
class BundleHeader:
    signature: str
    format_version: int
    player_version: str
    engine_version: str
    total_size: int | None
    flags: int | None
    compression: str | None


class _Reader:
    def __init__(self, data: bytes, pos: int) -> None:
        self.data = data
        self.pos = pos

    def unpack(self, fmt: str) -> tuple[int, ...]:
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise UnityFormatError(f"header truncated at offset {self.pos} reading {fmt}")
        values = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return values

    def cstring(self, limit: int) -> str:
        end = self.data.find(b"\x00", self.pos, self.pos + limit)
        if end < 0:
            raise UnityFormatError(f"unterminated string at offset {self.pos}")
        raw = self.data[self.pos:end]
        self.pos = end + 1
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise UnityFormatError(f"non-ASCII header string at offset {self.pos}: {raw[:32]!r}") from exc
        if not text.isprintable():
            raise UnityFormatError(f"non-printable header string at offset {self.pos}: {text!r}")
        return text


def parse_serialized_header(head: bytes, actual_size: int) -> SerializedFileHeader:
    """Parse the first bytes of a SerializedFile. `actual_size` is the on-disk size, used as a sanity check."""
    reader = _Reader(head, 0)
    metadata_size, file_size, version, data_offset = reader.unpack(">IIII")
    if not _MIN_FORMAT <= version <= _MAX_FORMAT:
        raise UnityFormatError(f"unsupported SerializedFile format version {version}")
    (endian_flag,) = reader.unpack(">B")
    reader.unpack(">3x")
    if version >= 22:
        metadata_size, file_size, data_offset = reader.unpack(">IQQ")
        reader.unpack(">8x")
    if file_size != actual_size:
        raise UnityFormatError(f"header file size {file_size} != actual size {actual_size}")
    if data_offset > file_size or metadata_size > file_size:
        raise UnityFormatError(f"data offset {data_offset} / metadata size {metadata_size} exceed file size {file_size}")

    endian = ">" if endian_flag else "<"
    unity_version = reader.cstring(64)
    (platform,) = reader.unpack(f"{endian}i")
    (type_tree,) = reader.unpack(">B") if version >= 13 else (0,)
    (type_count,) = reader.unpack(f"{endian}i")
    if type_count < 0:
        raise UnityFormatError(f"negative type count {type_count}")
    return SerializedFileHeader(
        format_version=version,
        metadata_size=metadata_size,
        file_size=file_size,
        data_offset=data_offset,
        big_endian=bool(endian_flag),
        unity_version=unity_version,
        target_platform=platform,
        target_platform_name=PLATFORM_NAMES.get(platform, f"unknown({platform})"),
        type_tree_enabled=bool(type_tree),
        type_count=type_count,
    )


def parse_bundle_header(head: bytes) -> BundleHeader:
    reader = _Reader(head, 0)
    signature = reader.cstring(16)
    if signature not in ("UnityFS", "UnityWeb", "UnityRaw", "UnityArchive"):
        raise UnityFormatError(f"not an AssetBundle signature: {signature!r}")
    (format_version,) = reader.unpack(">I")
    player_version = reader.cstring(64)
    engine_version = reader.cstring(64)
    if signature != "UnityFS":
        return BundleHeader(signature, format_version, player_version, engine_version, None, None, None)
    total_size, _compressed_info, _uncompressed_info, flags = reader.unpack(">qIII")
    compression = COMPRESSION_NAMES.get(flags & 0x3F, f"unknown({flags & 0x3F})")
    return BundleHeader(signature, format_version, player_version, engine_version, total_size, flags, compression)


def _read_head(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(_HEAD_BYTES)


def detect_kind(path: Path) -> FileKind:
    head = _read_head(path)
    if head.startswith(b"UnityFS\x00"):
        return FileKind.BUNDLE_UNITYFS
    if head.startswith(_LEGACY_BUNDLE_SIGNATURES):
        return FileKind.BUNDLE_LEGACY
    if path.suffix.lower() in _RESOURCE_SUFFIXES:
        return FileKind.RESOURCE
    if _looks_serialized(head, path.stat().st_size):
        return FileKind.SERIALIZED
    if path.suffix.lower() in _TEXT_SUFFIXES or path.name in _TEXT_NAMES or path.name == "boot.config":
        return FileKind.TEXT
    return FileKind.UNKNOWN


def _looks_serialized(head: bytes, actual_size: int) -> bool:
    if len(head) < 20:
        return False
    _meta, legacy_size, version, _offset = struct.unpack_from(">IIII", head, 0)
    if not _MIN_FORMAT <= version <= _MAX_FORMAT:
        return False
    if version >= 22:
        if len(head) < 48:
            return False
        (file_size,) = struct.unpack_from(">Q", head, 24)
        return file_size == actual_size
    return legacy_size == actual_size


def read_serialized_header(path: Path) -> SerializedFileHeader:
    try:
        return parse_serialized_header(_read_head(path), path.stat().st_size)
    except UnityFormatError as exc:
        raise UnityFormatError(f"{path}: {exc}") from exc


def read_bundle_header(path: Path) -> BundleHeader:
    try:
        return parse_bundle_header(_read_head(path))
    except UnityFormatError as exc:
        raise UnityFormatError(f"{path}: {exc}") from exc
