import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path

from uare.addressables import AddressablesCatalogError, asset_paths

GUID_A = "0123456789abcdef0123456789abcdef"
GUID_B = "fedcba9876543210fedcba9876543210"
GUID_C = "00000000000000000000000000000001"


def _key(text: str, unicode: bool = False) -> bytes:
    raw = text.encode("utf-16-le" if unicode else "ascii")
    return bytes([1 if unicode else 0]) + struct.pack("<i", len(raw)) + raw


def catalog(keys: list[bytes], buckets: list[list[int]], entry_ids: list[int], ids: list[str]) -> dict[str, object]:
    key_data, offsets = bytearray(struct.pack("<i", len(keys))), []
    for key in keys:
        offsets.append(len(key_data))
        key_data += key
    bucket_data = bytearray(struct.pack("<i", len(buckets)))
    for offset, entries in zip(offsets, buckets):
        bucket_data += struct.pack(f"<ii{len(entries)}i", offset, len(entries), *entries)
    entry_data = bytearray(struct.pack("<i", len(entry_ids)))
    for internal in entry_ids:
        entry_data += struct.pack("<7i", internal, 0, -1, 0, -1, 0, 0)
    encode = lambda b: base64.b64encode(bytes(b)).decode()
    return {"m_InternalIds": ids, "m_InternalIdPrefixes": [], "m_KeyDataString": encode(key_data),
            "m_BucketDataString": encode(bucket_data), "m_EntryDataString": encode(entry_data)}


class AddressablesCatalogTests(unittest.TestCase):
    def _parse(self, data: dict[str, object]):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(data))
            return asset_paths(path)

    def test_path_keys_map_to_the_guid_their_entries_load(self) -> None:
        data = catalog([_key("Assets/Prefab/Hero.prefab"), _key(GUID_A), _key("Assets/Scene/Main.unity", unicode=True)],
                       [[0], [0], [1]], [0, 1], [GUID_A, GUID_B])
        result = self._parse(data)
        self.assertEqual(result.paths, {GUID_A: "Assets/Prefab/Hero.prefab", GUID_B: "Assets/Scene/Main.unity"})
        self.assertEqual(result.ambiguous, 0)
        self.assertEqual(len(result.sha256), 64)

    def test_ambiguous_paths_and_guids_are_left_out(self) -> None:
        data = catalog([_key("Assets/Two.prefab"), _key("Assets/A.prefab"), _key("Assets/B.prefab")],
                       [[0, 1], [2], [2]], [0, 1, 2], [GUID_A, GUID_B, GUID_C])
        result = self._parse(data)
        self.assertEqual(result.paths, {})
        self.assertEqual(result.ambiguous, 2)

    def test_non_guid_internal_ids_are_ignored(self) -> None:
        data = catalog([_key("Assets/Remote.prefab")], [[0]], [0], ["https://cdn/remote_0123.bundle"])
        self.assertEqual(self._parse(data).paths, {})

    def test_malformed_tables_fail_loudly(self) -> None:
        data = catalog([_key("Assets/Hero.prefab")], [[5]], [0], [GUID_A])
        with self.assertRaises(AddressablesCatalogError):
            self._parse(data)
        data["m_BucketDataString"] = "not base64!"
        with self.assertRaises(AddressablesCatalogError):
            self._parse(data)

    def test_binary_catalog_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.bin"
            path.write_bytes(b"\x00\x01binary")
            with self.assertRaises(AddressablesCatalogError):
                asset_paths(path)


if __name__ == "__main__":
    unittest.main()
