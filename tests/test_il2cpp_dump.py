import struct
import tempfile
import unittest
from pathlib import Path

from fixtures import il2cpp_metadata_v31
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

    def test_unsupported_version_is_rejected(self) -> None:
        data = bytearray(il2cpp_metadata_v31(self._metadata()))
        struct.pack_into("<i", data, 4, 24)  # version 24
        path = self.tmp / "global-metadata.dat"
        path.write_bytes(bytes(data))
        (self.tmp / "out").mkdir()
        with self.assertRaisesRegex(Il2CppMetadataError, r"v\(29, 30, 31\)"):
            dump_definitions(path, self.tmp / "out")


if __name__ == "__main__":
    unittest.main()
