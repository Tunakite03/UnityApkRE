import struct
import tempfile
import unittest
from pathlib import Path

from fixtures import (
    il2cpp_metadata_grouped,
    il2cpp_metadata_v24_2,
    il2cpp_metadata_v31,
    il2cpp_metadata_v31_grouped,
)
from uare.errors import Il2CppMetadataError
from uare.il2cpp_dump import dump_definitions


class DumpDefinitionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _metadata(self) -> dict[int, list[tuple[str, str]]]:
        return {
            3: [("OnGameEnd", ""), ("FlightStarted", "")],
            4: [("Score", ""), ("Health", "")],
            5: [(".ctor", ""), ("UpdateShop", ""), ("Buy", "")],
            10: [("amount", ""), ("itemId", "")],
            11: [("m_coins", ""), ("m_level", "")],
            19: [("ShopPage", "Game.UI"), ("LobbyView", "Game.UI"), ("Player", "")],
        }

    def test_emits_categorised_name_tables(self) -> None:
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(il2cpp_metadata_v31(self._metadata()))
        out = self.tmp / "out"
        out.mkdir()
        result = dump_definitions(path, out)

        self.assertEqual(result.version, 31)
        names = {t.name: t for t in result.tables}
        self.assertEqual(names["methods"].count, 3)
        self.assertEqual(names["types"].count, 3)
        self.assertEqual([t.unreadable for t in result.tables], [0] * 6)

        events = (out / "events.tsv").read_text().splitlines()
        self.assertEqual(events, ["0\tOnGameEnd", "1\tFlightStarted"])
        types = (out / "types.tsv").read_text().splitlines()
        self.assertEqual(types[0], "0\tGame.UI\tShopPage")
        self.assertEqual(types[2], "2\t\tPlayer")
        import json
        sections = json.loads((out / "sections.json").read_text())
        self.assertEqual(sections["version"], 31)
        self.assertEqual(len(sections["sections"]), 31)

    def test_obfuscated_name_indices_are_counted_not_guessed(self) -> None:
        data = bytearray(il2cpp_metadata_v31(self._metadata()))
        pairs = [struct.unpack_from("<II", data, 8 + i * 8) for i in range(31)]
        methods_off = pairs[5][0]
        struct.pack_into("<i", data, methods_off + 0, 999999)  # nameIndex out of range
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(bytes(data))
        out = self.tmp / "out"
        out.mkdir()
        result = dump_definitions(path, out)
        methods = {t.name: t for t in result.tables}["methods"]
        self.assertEqual(methods.unreadable, 1)
        self.assertEqual((out / "methods.tsv").read_text().splitlines()[0], "0\t")
        self.assertIn("unreadable", result.note)

    def test_wrong_magic_reports_encryption(self) -> None:
        data = bytearray(il2cpp_metadata_v31(self._metadata()))
        data[0:4] = b"\xde\xad\xbe\xef"
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(bytes(data))
        (self.tmp / "out").mkdir()
        with self.assertRaisesRegex(Il2CppMetadataError, "encrypted"):
            dump_definitions(path, self.tmp / "out")

    def test_groups_members_under_their_type(self) -> None:
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(il2cpp_metadata_v31_grouped([
            {"name": "ShopPage", "namespace": "Game.UI", "methods": [".ctor", "Buy"],
             "fields": ["m_coins"], "events": ["OnGameEnd"], "properties": ["Coins"]},
            {"name": "Player", "namespace": "Game", "methods": ["Update"],
             "fields": ["m_hp", "m_mp"]},
        ]))
        out = self.tmp / "out"
        out.mkdir()
        result = dump_definitions(path, out)

        self.assertEqual(result.grouping.types_total, 2)
        self.assertEqual(result.grouping.types_grouped, 2)
        self.assertEqual(result.grouping.member_rows, 8)
        self.assertEqual(result.grouping.ranges_skipped, 0)
        self.assertEqual(result.grouping.valid_fraction, 1.0)

        rows = [line.split("\t") for line in (out / "members.tsv").read_text().splitlines()]
        # (type_index, type_name, kind, member_index, member_name)
        self.assertIn(["0", "ShopPage", "method", "1", "Buy"], rows)
        self.assertIn(["0", "ShopPage", "field", "0", "m_coins"], rows)
        self.assertIn(["0", "ShopPage", "event", "0", "OnGameEnd"], rows)
        self.assertIn(["0", "ShopPage", "property", "0", "Coins"], rows)
        self.assertIn(["1", "Player", "method", "2", "Update"], rows)
        self.assertIn(["1", "Player", "field", "2", "m_mp"], rows)

    def test_member_range_that_does_not_fit_is_skipped_not_guessed(self) -> None:
        data = bytearray(il2cpp_metadata_v31_grouped([
            {"name": "Good", "namespace": "", "methods": ["M"], "fields": ["F"]},
        ]))
        pairs = [struct.unpack_from("<II", data, 8 + i * 8) for i in range(31)]
        types_off = pairs[19][0]
        struct.pack_into("<i", data, types_off + 36, 5000)  # methodStart past the method table (v31)
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(bytes(data))
        out = self.tmp / "out"
        out.mkdir()
        result = dump_definitions(path, out)

        self.assertEqual(result.grouping.ranges_skipped, 1)
        rows = (out / "members.tsv").read_text().splitlines()
        self.assertEqual(rows, ["0\tGood\tfield\t0\tF"])
        self.assertNotIn("\tmethod\t", "\n".join(rows))
        self.assertIn("skipped", result.note)

    def test_unsupported_version_is_rejected(self) -> None:
        data = bytearray(il2cpp_metadata_v31(self._metadata()))
        struct.pack_into("<i", data, 4, 23)  # version 23 is below the supported floor
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(bytes(data))
        (self.tmp / "out").mkdir()
        with self.assertRaisesRegex(Il2CppMetadataError, "supports metadata"):
            dump_definitions(path, self.tmp / "out")

    def test_v27_layout_groups_members(self) -> None:
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(il2cpp_metadata_grouped(27, [
            {"name": "ShopPage", "namespace": "Game.UI", "methods": [".ctor", "Buy"],
             "fields": ["m_coins"], "events": ["OnGameEnd"]},
            {"name": "Player", "namespace": "Game", "methods": ["Update"], "fields": ["m_hp", "m_mp"]},
        ]))
        out = self.tmp / "out"
        out.mkdir()
        result = dump_definitions(path, out)
        self.assertEqual(result.layout, "v27")
        self.assertEqual(result.grouping.member_rows, 7)
        rows = [line.split("\t") for line in (out / "members.tsv").read_text().splitlines()]
        self.assertIn(["1", "Player", "method", "2", "Update"], rows)

    def test_v24_layout_groups_members(self) -> None:
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(il2cpp_metadata_grouped(24, [
            {"name": "ShopPage", "namespace": "Game.UI", "methods": [".ctor", "Buy"],
             "fields": ["m_coins"]},
        ]))
        out = self.tmp / "out"
        out.mkdir()
        result = dump_definitions(path, out)
        self.assertEqual(result.layout, "v24")
        self.assertEqual(result.grouping.member_rows, 3)
        rows = [line.split("\t") for line in (out / "members.tsv").read_text().splitlines()]
        self.assertIn(["0", "ShopPage", "field", "0", "m_coins"], rows)

    def test_v24_2_subversion_is_detected_by_header_size(self) -> None:
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(il2cpp_metadata_v24_2([
            {"name": "ShopPage", "namespace": "Game.UI", "methods": [".ctor", "Buy"], "fields": ["m_coins"]},
        ]))
        out = self.tmp / "out"
        out.mkdir()
        result = dump_definitions(path, out)
        self.assertEqual(result.layout, "v24.2")
        self.assertEqual(result.grouping.member_rows, 3)

    def test_wrong_layout_for_version_raises_not_garbage(self) -> None:
        # A v24 header (33 pairs) but v31 strides: the tables must fail validation, not emit names.
        data = bytearray(il2cpp_metadata_grouped(24, [{"name": "X", "namespace": "", "methods": ["M"]}]))
        struct.pack_into("<i", data, 4, 31)  # claim v31; the file is really v24
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(bytes(data))
        (self.tmp / "out").mkdir()
        with self.assertRaises(Il2CppMetadataError):
            dump_definitions(path, self.tmp / "out")


if __name__ == "__main__":
    unittest.main()
