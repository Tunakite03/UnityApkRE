import json
import struct
import tempfile
import unittest
from pathlib import Path

from fixtures import (
    mono_script_body,
    serialized_v17_objects,
    serialized_v22_objects,
    text_asset_body,
)
from uare.assets import analyze_assets
from uare.errors import UnityFormatError
from uare.serialized import parse_serialized_file, read_serialized_file


class SerializedParseTests(unittest.TestCase):
    def test_parses_object_table(self) -> None:
        blob = serialized_v22_objects("6000.2.15f1", [
            (1, 49, text_asset_body("Config", b"a,b,c")),
            (2, 114, b"\x00" * 32),
            (3, 28, b"\x00" * 16),
        ])
        sf = parse_serialized_file(blob)
        self.assertEqual(sf.format_version, 22)
        self.assertEqual(sf.unity_version, "6000.2.15f1")
        self.assertEqual([o.path_id for o in sf.objects], [1, 2, 3])
        self.assertEqual([o.class_id(sf.types) for o in sf.objects], [49, 114, 28])
        for info in sf.objects:
            self.assertGreaterEqual(info.byte_start, sf.data_offset)
            self.assertLessEqual(info.byte_start + info.byte_size, sf.file_size)

    def test_parses_legacy_format_17_object_table(self) -> None:
        blob = serialized_v17_objects("2018.4.36f1", [
            (1, 49, text_asset_body("Config", b"a,b,c")),
            (2, 28, text_asset_body("icon_coin", b"")),
        ])
        sf = parse_serialized_file(blob)
        self.assertEqual(sf.format_version, 17)
        self.assertEqual(sf.unity_version, "2018.4.36f1")
        self.assertEqual([o.class_id(sf.types) for o in sf.objects], [49, 28])
        for info in sf.objects:
            self.assertGreaterEqual(info.byte_start, sf.data_offset)
            self.assertLessEqual(info.byte_start + info.byte_size, sf.file_size)

    def test_format_16_is_still_rejected(self) -> None:
        blob = bytearray(serialized_v17_objects("5.6.7f1", [(1, 49, text_asset_body("X", b"y"))]))
        struct.pack_into(">I", blob, 8, 16)  # version field -> 16
        with self.assertRaisesRegex(UnityFormatError, "format >="):
            parse_serialized_file(bytes(blob))

    def test_type_tree_file_is_rejected(self) -> None:
        blob = bytearray(serialized_v22_objects("2022.3.1f1", [(1, 49, text_asset_body("X", b"y"))]))
        # flip the type-tree-enabled byte (right after the unity version string + platform int)
        idx = blob.index(b"2022.3.1f1\x00") + len("2022.3.1f1\x00") + 4
        blob[idx] = 1
        with self.assertRaisesRegex(UnityFormatError, "type tree"):
            parse_serialized_file(bytes(blob))

    def test_corrupt_object_range_fails_loud(self) -> None:
        blob = bytearray(serialized_v22_objects("6000.2.15f1", [(1, 49, text_asset_body("X", b"y"))]))
        # Corrupt file_size in the header (big-endian u64 at offset 24) to something smaller.
        struct.pack_into(">Q", blob, 24, 10)
        with self.assertRaises(UnityFormatError):
            parse_serialized_file(bytes(blob))


class AnalyzeAssetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "Data"
        self.data.mkdir()
        (self.data / "globalgamemanagers").write_bytes(b"gg")  # layout marker; too short to parse as an object file

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, objects: list[tuple[int, int, bytes]]) -> None:
        (self.data / name).write_bytes(serialized_v22_objects("6000.2.15f1", objects))

    def test_extracts_text_assets_objects_and_scripts(self) -> None:
        self._write("sharedassets0.assets", [
            (1, 49, text_asset_body("StarChallenge", b"Id,value\n0,70\n")),
            (2, 49, text_asset_body("AfkConfig", b"rate=5")),
            (3, 115, mono_script_body("ShopPage", "ShopPage", "Game.Shop", "Assembly-CSharp")),
            (4, 28, text_asset_body("icon_coin", b"")),  # Texture2D: m_Name first
            (5, 1, struct.pack("<i", 2)),  # GameObject: not a named class, no name read
        ])
        out = self.tmp / "out"
        result = analyze_assets(self.data, out)

        self.assertEqual(result.serialized_files, 1)
        self.assertEqual(result.objects, 5)
        self.assertEqual(result.text_assets_written, 2)
        self.assertEqual(result.mono_scripts, 1)
        self.assertEqual(result.class_counts["TextAsset"], 2)
        self.assertEqual(result.class_counts["GameObject"], 1)

        self.assertEqual((out / "textassets/StarChallenge").read_bytes(), b"Id,value\n0,70\n")
        self.assertEqual((out / "textassets/AfkConfig").read_text(), "rate=5")
        mono = (out / "monoscripts.tsv").read_text().strip().split("\t")
        self.assertEqual(mono[2:], ["ShopPage", "ShopPage", "Game.Shop", "Assembly-CSharp"])

        records = [json.loads(line) for line in (out / "objects.jsonl").read_text().splitlines()]
        by_class = {r["class_name"]: r for r in records}
        self.assertEqual(by_class["Texture2D"]["name"], "icon_coin")
        self.assertEqual(by_class["GameObject"]["name"], "")

    def test_extracts_legacy_format_17_assets(self) -> None:
        (self.data / "sharedassets0.assets").write_bytes(serialized_v17_objects("2018.4.36f1", [
            (1, 49, text_asset_body("LegacyConfig", b"k=v\n")),
        ]))
        out = self.tmp / "out"
        result = analyze_assets(self.data, out)
        self.assertEqual(result.objects, 1)
        self.assertEqual((out / "textassets/LegacyConfig").read_bytes(), b"k=v\n")

    def test_duplicate_names_do_not_overwrite(self) -> None:
        body = text_asset_body("Shared", b"one")
        self._write("a.assets", [(1, 49, body)])
        self._write("b.assets", [(1, 49, text_asset_body("Shared", b"two"))])
        out = self.tmp / "out"
        result = analyze_assets(self.data, out)
        self.assertEqual(result.text_assets_written, 2)
        written = sorted(p.read_bytes() for p in (out / "textassets").iterdir())
        self.assertEqual(written, [b"one", b"two"])

    def test_non_unity_root_is_rejected(self) -> None:
        from uare.errors import UareError
        empty = self.tmp / "not_unity"
        empty.mkdir()
        with self.assertRaises(UareError):
            analyze_assets(empty, self.tmp / "out")


if __name__ == "__main__":
    unittest.main()
