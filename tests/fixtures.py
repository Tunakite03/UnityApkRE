"""Builders for synthetic Unity/IL2CPP/ELF/APK inputs used by the tests."""

import struct
import zipfile
from pathlib import Path
from typing import Mapping

METADATA_MAGIC = 0xFAB11BAF


def serialized_v22(unity_version: str, platform: int, type_tree: bool, type_count: int, total_size: int) -> bytes:
    """SerializedFile format 22 header followed by zero padding up to total_size."""
    metadata = unity_version.encode() + b"\x00" + struct.pack("<i", platform) + bytes([type_tree])
    metadata += struct.pack("<i", type_count)
    head = struct.pack(">IIII", 0, 0, 22, 0) + bytes([0, 0, 0, 0])
    head += struct.pack(">IQQ", len(metadata), total_size, 4096) + b"\x00" * 8
    body = head + metadata
    if len(body) > total_size:
        raise ValueError("total_size too small for header")
    return body + b"\x00" * (total_size - len(body))


def serialized_v17(unity_version: str, platform: int, type_count: int, total_size: int) -> bytes:
    metadata = unity_version.encode() + b"\x00" + struct.pack("<i", platform) + b"\x01" + struct.pack("<i", type_count)
    head = struct.pack(">IIII", len(metadata), total_size, 17, 4096) + bytes([0, 0, 0, 0])
    body = head + metadata
    return body + b"\x00" * (total_size - len(body))


def unityfs_bundle(flags: int) -> bytes:
    return (b"UnityFS\x00" + struct.pack(">I", 7) + b"5.x.x\x00" + b"2021.3.45f1\x00"
            + struct.pack(">qIII", 1234, 100, 200, flags) + b"\x00" * 64)


def il2cpp_metadata(version: int, literals: list[bytes], heap: bytes) -> bytes:
    header_size = 8 + 6 * 4
    literal_data = b"".join(literals)
    table = b""
    cursor = 0
    for literal in literals:
        table += struct.pack("<Ii", len(literal), cursor)
        cursor += len(literal)
    table_off = header_size
    data_off = table_off + len(table)
    heap_off = data_off + len(literal_data)
    header = struct.pack("<Ii", METADATA_MAGIC, version)
    header += struct.pack("<6I", table_off, len(table), data_off, len(literal_data), heap_off, len(heap))
    return header + table + literal_data + heap


def elf64(exec_payload: bytes, symbols: list[str], imports: list[str]) -> bytes:
    """Little-endian aarch64 ELF64 with one R-X PT_LOAD segment and a .dynsym/.dynstr pair.

    `symbols` are defined (st_shndx=1); `imports` are undefined (st_shndx=SHN_UNDEF).
    """
    ehdr_size, phdr_size, shdr_size, sym_size = 64, 56, 64, 24
    payload_off = ehdr_size + phdr_size
    dynstr = b"\x00"
    entries: list[tuple[int, int]] = []
    for names, shndx in ((symbols, 1), (imports, 0)):
        for name in names:
            entries.append((len(dynstr), shndx))
            dynstr += name.encode() + b"\x00"
    dynsym = b"\x00" * sym_size
    for off, shndx in entries:
        dynsym += struct.pack("<IBBHQQ", off, 0x12, 0, shndx, 0x1000 if shndx else 0, 0)
    dynstr_off = payload_off + len(exec_payload)
    dynsym_off = dynstr_off + len(dynstr)
    shoff = dynsym_off + len(dynsym)
    ehdr = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    ehdr += struct.pack("<HHIQQQIHHHHHH", 3, 183, 1, 0, ehdr_size, shoff, 0, ehdr_size, phdr_size, 1,
                        shdr_size, 3, 0)
    phdr = struct.pack("<IIQQQQQQ", 1, 5, payload_off, 0, 0, len(exec_payload), len(exec_payload), 0x1000)
    shdrs = b"\x00" * shdr_size
    shdrs += struct.pack("<IIQQQQIIQQ", 0, 3, 0, 0, dynstr_off, len(dynstr), 0, 0, 1, 0)
    shdrs += struct.pack("<IIQQQQIIQQ", 0, 11, 0, 0, dynsym_off, len(dynsym), 1, 1, 8, sym_size)
    return ehdr + phdr + exec_payload + dynstr + dynsym + shdrs


def il2cpp_metadata_v31(tables: dict[int, list[tuple[str, str]]], extra_names: list[str] = []) -> bytes:
    """Build a metadata v31 file with a 31-pair header and the six definition tables.

    `tables` maps a section index (3 events, 4 properties, 5 methods, 10 parameters, 11 fields,
    19 types) to a list of (name, namespace) records. Records use nameIndex@0 and, for types,
    namespaceIndex@4. Strides match the real layout: events 24, properties 20, methods 36,
    parameters 12, fields 12, types 88.
    """
    strides = {3: 24, 4: 20, 5: 36, 10: 12, 11: 12, 19: 88}
    heap = bytearray(b"\x00")  # offset 0 is the empty string
    offsets: dict[str, int] = {}

    def intern(text: str) -> int:
        if text == "":
            return 0
        if text not in offsets:
            offsets[text] = len(heap)
            heap.extend(text.encode("utf-8") + b"\x00")
        return offsets[text]

    for records in tables.values():
        for name, namespace in records:
            intern(name)
            intern(namespace)
    for name in extra_names:
        intern(name)

    header_size = 8 + 31 * 8
    body = bytearray()
    pairs: list[tuple[int, int]] = [(header_size, 0)] * 31

    def place(index: int, blob: bytes) -> None:
        pairs[index] = (header_size + len(body), len(blob))
        body.extend(blob)

    place(2, bytes(heap))  # string section
    for index, records in tables.items():
        stride = strides[index]
        blob = bytearray()
        for name, namespace in records:
            record = bytearray(stride)
            struct.pack_into("<i", record, 0, intern(name))
            if index == 19:
                struct.pack_into("<i", record, 4, intern(namespace))
            blob.extend(record)
        place(index, bytes(blob))

    header = bytearray(struct.pack("<Ii", METADATA_MAGIC, 31))
    for offset, size in pairs:
        header.extend(struct.pack("<II", offset, size))
    assert len(header) == header_size
    return bytes(header) + bytes(body)


def serialized_v22_objects(unity_version: str, objects: list[tuple[int, int, bytes]]) -> bytes:
    """Build a SerializedFile v22 with one type per object. `objects` is (path_id, class_id, body)."""
    endian = "<"
    types = bytearray()
    for _path_id, class_id, _body in objects:
        types += struct.pack("<i", class_id) + bytes([0]) + struct.pack("<h", -1)
        if class_id == 114:
            types += b"\x00" * 16
        types += b"\x00" * 16

    data = bytearray()
    entries: list[tuple[int, int, int, int]] = []
    for type_index, (path_id, _class_id, bodyb) in enumerate(objects):
        while len(data) % 8:
            data.append(0)
        entries.append((path_id, len(data), len(bodyb), type_index))
        data.extend(bodyb)

    meta = bytearray()
    meta += unity_version.encode() + b"\x00"
    meta += struct.pack(endian + "i", 13)  # platform Android
    meta += bytes([0])  # type tree disabled
    meta += struct.pack(endian + "i", len(objects))
    meta += types
    meta += struct.pack(endian + "i", len(objects))
    for path_id, start, size, type_index in entries:
        while len(meta) % 4:
            meta.append(0)
        meta += struct.pack(endian + "q", path_id)
        meta += struct.pack(endian + "Q", start)
        meta += struct.pack(endian + "Ii", size, type_index)

    header_fixed = 48
    data_offset = header_fixed + len(meta)
    file_size = data_offset + len(data)
    header = struct.pack(">IIII", len(meta), file_size, 22, 0) + bytes([0, 0, 0, 0])
    header += struct.pack(">IQQ", len(meta), file_size, data_offset) + b"\x00" * 8
    assert len(header) == header_fixed
    return bytes(header) + bytes(meta) + bytes(data)


def unity_aligned_string(text: bytes) -> bytes:
    out = struct.pack("<I", len(text)) + text
    while len(out) % 4:
        out += b"\x00"
    return out


def text_asset_body(name: str, payload: bytes) -> bytes:
    return unity_aligned_string(name.encode()) + unity_aligned_string(payload)


def mono_script_body(name: str, class_name: str, namespace: str, assembly: str) -> bytes:
    body = unity_aligned_string(name.encode())
    body += struct.pack("<i", 0)  # m_ExecutionOrder
    body += b"\x00" * 16  # m_PropertiesHash
    body += unity_aligned_string(class_name.encode())
    body += unity_aligned_string(namespace.encode())
    body += unity_aligned_string(assembly.encode())
    return body


def write_zip(path: Path, entries: Mapping[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(zipfile.ZipInfo(name), data)
    return path
