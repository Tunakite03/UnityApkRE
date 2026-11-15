"""Asset paths recorded in an Addressables content catalog (catalog_*.json).

Addressables builds that name bundle contents by asset GUID keep the original project paths only in
the catalog: path addresses are keys, and their entries point at internal ids that are the asset
GUIDs. This module joins key -> bucket -> entry -> internal id and returns the GUID -> path pairs
that the catalog states unambiguously. Nothing is guessed: a path whose entries carry several GUIDs,
or a GUID claimed by several paths, is counted as ambiguous and left out.

The binary layout follows ContentCatalogData: base64 key, bucket and entry tables of little-endian
int32 values; each key is read at its bucket's data offset. Only string keys are decoded.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import struct
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError

_GUID = re.compile(r"[0-9a-f]{32}")
_ENTRY_FIELDS = 7  # internalId, provider, dependencyKey, depHash, dataIndex, primaryKey, resourceType
_ASCII, _UNICODE = 0, 1


class AddressablesCatalogError(UareError):
    """The Addressables catalog is missing, malformed or not the supported JSON layout."""


@dataclass(frozen=True)
class AddressableAssetPaths:
    source: str
    sha256: str
    paths: dict[str, str]  # asset GUID -> original asset path
    ambiguous: int


def _table(catalog: dict[str, object], name: str) -> bytes:
    value = catalog.get(name)
    if not isinstance(value, str):
        raise AddressablesCatalogError(f"catalog field {name} is missing or not a string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AddressablesCatalogError(f"catalog field {name} is not valid base64") from exc


def _int(data: bytes, offset: int, name: str) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise AddressablesCatalogError(f"{name}: read past end at offset {offset}")
    return struct.unpack_from("<i", data, offset)[0]


def _key(data: bytes, offset: int) -> str | None:
    """The string key stored at offset; None for non-string key types."""
    if offset < 0 or offset >= len(data):
        raise AddressablesCatalogError(f"key offset {offset} outside key data")
    kind = data[offset]
    if kind not in (_ASCII, _UNICODE):
        return None
    length = _int(data, offset + 1, "key length")
    start = offset + 5
    if length < 0 or start + length > len(data):
        raise AddressablesCatalogError(f"key at offset {offset} has invalid length {length}")
    raw = data[start:start + length]
    try:
        return raw.decode("ascii") if kind == _ASCII else raw.decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise AddressablesCatalogError(f"key at offset {offset} is not valid text") from exc


def _buckets(data: bytes) -> list[tuple[int, tuple[int, ...]]]:
    count = _int(data, 0, "bucket count")
    if count < 0:
        raise AddressablesCatalogError(f"negative bucket count {count}")
    offset, buckets = 4, []
    for _ in range(count):
        key_offset = _int(data, offset, "bucket key offset")
        entry_count = _int(data, offset + 4, "bucket entry count")
        if entry_count < 0 or offset + 8 + 4 * entry_count > len(data):
            raise AddressablesCatalogError(f"bucket at offset {offset} has invalid entry count {entry_count}")
        buckets.append((key_offset, struct.unpack_from(f"<{entry_count}i", data, offset + 8)))
        offset += 8 + 4 * entry_count
    return buckets


def _internal_ids(catalog: dict[str, object]) -> list[str]:
    ids = catalog.get("m_InternalIds")
    prefixes = catalog.get("m_InternalIdPrefixes", [])
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise AddressablesCatalogError("catalog field m_InternalIds is missing or not a string list")
    if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
        raise AddressablesCatalogError("catalog field m_InternalIdPrefixes is not a string list")
    expanded = []
    for internal in ids:
        head, sep, rest = internal.partition("#")
        if sep and prefixes and head.isdigit() and int(head) < len(prefixes):
            internal = prefixes[int(head)] + rest
        expanded.append(internal)
    return expanded


def asset_paths(catalog_path: Path) -> AddressableAssetPaths:
    try:
        raw = catalog_path.read_bytes()
        catalog = json.loads(raw)
    except OSError as exc:
        raise AddressablesCatalogError(f"cannot read catalog {catalog_path}") from exc
    except json.JSONDecodeError as exc:
        raise AddressablesCatalogError(f"catalog {catalog_path} is not JSON (binary catalogs are not supported)") from exc
    if not isinstance(catalog, dict):
        raise AddressablesCatalogError(f"catalog {catalog_path} is not a JSON object")
    keys, entries = _table(catalog, "m_KeyDataString"), _table(catalog, "m_EntryDataString")
    ids = _internal_ids(catalog)
    entry_count = _int(entries, 0, "entry count")
    if entry_count < 0 or 4 + entry_count * _ENTRY_FIELDS * 4 > len(entries):
        raise AddressablesCatalogError(f"entry table too short for {entry_count} entries")

    def internal_id(entry: int) -> str:
        if not 0 <= entry < entry_count:
            raise AddressablesCatalogError(f"bucket references missing entry {entry}")
        index = _int(entries, 4 + entry * _ENTRY_FIELDS * 4, "entry internal id")
        if not 0 <= index < len(ids):
            raise AddressablesCatalogError(f"entry {entry} references missing internal id {index}")
        return ids[index]

    claims: dict[str, set[str]] = defaultdict(set)
    ambiguous_paths = 0
    for key_offset, bucket_entries in _buckets(_table(catalog, "m_BucketDataString")):
        key = _key(keys, key_offset)
        if key is None or not key.startswith("Assets/"):
            continue
        guids = {i for i in map(internal_id, bucket_entries) if _GUID.fullmatch(i)}
        if len(guids) == 1:
            claims[guids.pop()].add(key)
        elif guids:
            ambiguous_paths += 1
    paths = {guid: next(iter(found)) for guid, found in claims.items() if len(found) == 1}
    return AddressableAssetPaths(str(catalog_path), hashlib.sha256(raw).hexdigest(), paths,
                                 ambiguous_paths + sum(len(found) > 1 for found in claims.values()))
