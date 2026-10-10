"""Full SerializedFile metadata parse: the type table and the object table.

unity.py reads only the header. This module continues into the metadata section to recover the
object table — for each object its class id and its [byteStart, byteStart+byteSize) range inside
the data section. That is enough to locate and read individual assets (assets.py) without a type
tree.

Supported: format 22+ (Unity 2020.2+), type-tree-disabled player builds, little- or big-endian.
Every parsed object range is validated against [data_offset, file_size]; a layout that does not
fit raises UnityFormatError rather than returning garbage. Type trees (editor files) and formats
below 22 are reported as unsupported for object parsing; header inspection still works via unity.py.
"""

import struct
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UnityFormatError

_MIN_OBJECT_FORMAT = 22
_COMMON_TYPE_HASH = 16
_SCRIPT_TYPE_HASH = 16


@dataclass(frozen=True)
class SerializedType:
    class_id: int
    is_stripped: bool
    script_type_index: int


@dataclass(frozen=True)
class ObjectInfo:
    path_id: int
    byte_start: int  # absolute offset in the file (data_offset already added)
    byte_size: int
    type_index: int

    def class_id(self, types: "tuple[SerializedType, ...]") -> int:
        if 0 <= self.type_index < len(types):
            return types[self.type_index].class_id
        return -1


@dataclass(frozen=True)
class SerializedFile:
    format_version: int
    big_endian: bool
    unity_version: str
    data_offset: int
    file_size: int
    types: tuple[SerializedType, ...]
    objects: tuple[ObjectInfo, ...]


class _Cursor:
    def __init__(self, data: bytes, pos: int, endian: str) -> None:
        self.data = data
        self.pos = pos
        self.endian = endian

    def unpack(self, fmt: str) -> tuple[int, ...]:
        fmt = self.endian + fmt
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise UnityFormatError(f"metadata truncated at offset {self.pos} reading {fmt!r}")
        values = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return values

    def skip(self, count: int) -> None:
        if self.pos + count > len(self.data):
            raise UnityFormatError(f"metadata truncated skipping {count} bytes at {self.pos}")
        self.pos += count

    def align(self) -> None:
        self.pos = (self.pos + 3) & ~3


def _read_header(data: bytes) -> tuple[int, str, int, int, int]:
    """Return (format_version, endian, data_offset, file_size, cursor position after header)."""
    if len(data) < 48:
        raise UnityFormatError("file too small for a SerializedFile header")
    _metadata_size, _legacy_size, version, _legacy_offset = struct.unpack_from(">IIII", data, 0)
    if version < _MIN_OBJECT_FORMAT:
        raise UnityFormatError(f"object parsing needs SerializedFile format >= {_MIN_OBJECT_FORMAT}, got {version}")
    endian_flag = data[16]
    endian = ">" if endian_flag else "<"
    _metadata_size2, file_size, data_offset = struct.unpack_from(">IQQ", data, 20)
    pos = 20 + 4 + 8 + 8 + 8  # reserved 8 bytes follow data_offset in v22+
    return version, endian, data_offset, file_size, pos


def parse_serialized_file(data: bytes) -> SerializedFile:
    version, endian, data_offset, file_size, pos = _read_header(data)
    if file_size != len(data):
        raise UnityFormatError(f"header file size {file_size} != actual {len(data)}")
    cursor = _Cursor(data, pos, endian)

    end = data.find(b"\x00", cursor.pos, cursor.pos + 64)
    if end < 0:
        raise UnityFormatError("unterminated or over-long Unity version string in header")
    unity_version = data[cursor.pos:end].decode("ascii", errors="replace")
    cursor.pos = end + 1
    (_target_platform,) = cursor.unpack("i")
    (type_tree_enabled,) = cursor.unpack("B")
    if type_tree_enabled:
        raise UnityFormatError("SerializedFile embeds type trees (editor file); object parsing not supported")

    (type_count,) = cursor.unpack("i")
    if type_count < 0 or type_count > (len(data) // 4):
        raise UnityFormatError(f"implausible type count {type_count}")
    types: list[SerializedType] = []
    for _ in range(type_count):
        (class_id,) = cursor.unpack("i")
        (is_stripped,) = cursor.unpack("B")
        (script_type_index,) = cursor.unpack("h")
        if class_id == 114:  # MonoBehaviour carries a 16-byte script hash
            cursor.skip(_SCRIPT_TYPE_HASH)
        cursor.skip(_COMMON_TYPE_HASH)
        types.append(SerializedType(class_id=class_id, is_stripped=bool(is_stripped),
                                    script_type_index=script_type_index))

    (object_count,) = cursor.unpack("i")
    if object_count < 0 or object_count > (len(data) // 12):
        raise UnityFormatError(f"implausible object count {object_count}")
    objects: list[ObjectInfo] = []
    for _ in range(object_count):
        cursor.align()
        (path_id,) = cursor.unpack("q")
        (byte_start,) = cursor.unpack("Q")
        (byte_size, type_index) = cursor.unpack("Ii")
        absolute = data_offset + byte_start
        if absolute < data_offset or absolute + byte_size > file_size:
            raise UnityFormatError(
                f"object {path_id} range [{absolute}, +{byte_size}) outside data section [{data_offset}, {file_size})"
            )
        if type_index < 0 or type_index >= type_count:
            raise UnityFormatError(f"object {path_id} type index {type_index} out of range (types={type_count})")
        objects.append(ObjectInfo(path_id=path_id, byte_start=absolute, byte_size=byte_size, type_index=type_index))

    # Unity packs object bodies contiguously and never overlaps them. Rejecting overlap stops a
    # hostile file from pointing many objects at one large body to force quadratic reads/writes.
    for prev, nxt in zip(sorted(objects, key=lambda o: o.byte_start),
                         sorted(objects, key=lambda o: o.byte_start)[1:]):
        if nxt.byte_start < prev.byte_start + prev.byte_size:
            raise UnityFormatError(
                f"object bodies overlap ({prev.path_id} and {nxt.path_id}); malformed or hostile file")

    return SerializedFile(format_version=version, big_endian=(endian == ">"), unity_version=unity_version,
                          data_offset=data_offset, file_size=file_size, types=tuple(types), objects=tuple(objects))


def read_serialized_file(path: Path) -> SerializedFile:
    try:
        return parse_serialized_file(path.read_bytes())
    except UnityFormatError as exc:
        raise UnityFormatError(f"{path}: {exc}") from exc


class ObjectReader:
    """Reads primitive fields of one object's body. Aligned-string and PPtr layouts are stable
    across versions for the simple asset types assets.py handles."""

    def __init__(self, file: SerializedFile, data: bytes, info: ObjectInfo) -> None:
        self._endian = ">" if file.big_endian else "<"
        self._data = data
        self._start = info.byte_start
        self._end = info.byte_start + info.byte_size
        self.pos = info.byte_start

    def _check(self, size: int) -> None:
        if self.pos + size > self._end:
            raise UnityFormatError(f"object field at {self.pos} runs past its {self._end - self._start}-byte body")

    def align(self) -> None:
        rel = self.pos - self._start
        self.pos = self._start + ((rel + 3) & ~3)

    def i32(self) -> int:
        self._check(4)
        (value,) = struct.unpack_from(self._endian + "i", self._data, self.pos)
        self.pos += 4
        return value

    def u32(self) -> int:
        self._check(4)
        (value,) = struct.unpack_from(self._endian + "I", self._data, self.pos)
        self.pos += 4
        return value

    def string(self) -> bytes:
        length = self.u32()
        if length < 0 or self.pos + length > self._end:
            raise UnityFormatError(f"string length {length} runs past the object body at {self.pos}")
        raw = self._data[self.pos:self.pos + length]
        self.pos += length
        self.align()
        return raw

    def byte_array(self) -> bytes:
        length = self.u32()
        if length < 0 or self.pos + length > self._end:
            raise UnityFormatError(f"byte array length {length} runs past the object body at {self.pos}")
        raw = self._data[self.pos:self.pos + length]
        self.pos += length
        self.align()
        return raw
