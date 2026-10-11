import struct
import tempfile
import unittest
from pathlib import Path

from uare.wwise import (
    WwiseFormatError,
    _parse_bkhd,
    _parse_didx,
    _read_sections,
    extract_bank,
    extract_all,
)


def _make_bank(version: int = 134, bank_id: int = 42, wems: list[bytes] | None = None) -> bytes:
    """Build a minimal .bnk with BKHD + DIDX + DATA sections."""
    if wems is None:
        wems = [b"\xff\xfe" * 10, b"\xab\xcd" * 5]
    bkhd_body = struct.pack("<II", version, bank_id) + b"\x00" * 8
    bkhd = b"BKHD" + struct.pack("<I", len(bkhd_body)) + bkhd_body

    didx_entries = b""
    data_body = b""
    offset = 0
    for i, wem in enumerate(wems):
        didx_entries += struct.pack("<III", 1000 + i, offset, len(wem))
        data_body += wem
        offset += len(wem)
    didx = b"DIDX" + struct.pack("<I", len(didx_entries)) + didx_entries
    data = b"DATA" + struct.pack("<I", len(data_body)) + data_body
    return bkhd + didx + data


class ReadSectionsTests(unittest.TestCase):
    def test_parse_basic_bank(self):
        raw = _make_bank()
        sections = _read_sections(raw)
        self.assertIn("BKHD", sections)
        self.assertIn("DIDX", sections)
        self.assertIn("DATA", sections)

    def test_empty_data(self):
        sections = _read_sections(b"")
        self.assertEqual(sections, {})


class ParseBkhdTests(unittest.TestCase):
    def test_parse_version_and_id(self):
        body = struct.pack("<II", 134, 99) + b"\x00" * 8
        version, bank_id = _parse_bkhd(body, 0, len(body))
        self.assertEqual(version, 134)
        self.assertEqual(bank_id, 99)

    def test_short_bkhd_raises(self):
        with self.assertRaises(WwiseFormatError):
            _parse_bkhd(b"\x00" * 4, 0, 4)


class ParseDidxTests(unittest.TestCase):
    def test_parse_two_entries(self):
        entry1 = struct.pack("<III", 100, 0, 20)
        entry2 = struct.pack("<III", 101, 20, 10)
        entries = _parse_didx(entry1 + entry2, 0, len(entry1 + entry2))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], (100, 0, 20))
        self.assertEqual(entries[1], (101, 20, 10))

    def test_bad_length_raises(self):
        with self.assertRaises(WwiseFormatError):
            _parse_didx(b"\x00" * 13, 0, 13)


class ExtractBankTests(unittest.TestCase):
    def test_extracts_wem_files(self):
        wem1 = b"\xff" * 20
        wem2 = b"\xab" * 10
        raw = _make_bank(wems=[wem1, wem2])
        with tempfile.TemporaryDirectory() as tmpdir:
            bnk = Path(tmpdir) / "test.bnk"
            bnk.write_bytes(raw)
            out = Path(tmpdir) / "out"
            result = extract_bank(bnk, out)
            self.assertEqual(result.wem_count, 2)
            self.assertEqual(result.version, 134)
            self.assertEqual(result.bank_id, 42)
            self.assertTrue((out / "1000.wem").is_file())
            self.assertTrue((out / "1001.wem").is_file())
            self.assertEqual((out / "1000.wem").read_bytes(), wem1)
            self.assertEqual((out / "1001.wem").read_bytes(), wem2)

    def test_bank_without_data(self):
        bkhd_body = struct.pack("<II", 100, 1) + b"\x00" * 8
        raw = b"BKHD" + struct.pack("<I", len(bkhd_body)) + bkhd_body
        with tempfile.TemporaryDirectory() as tmpdir:
            bnk = Path(tmpdir) / "empty.bnk"
            bnk.write_bytes(raw)
            out = Path(tmpdir) / "out"
            result = extract_bank(bnk, out)
            self.assertEqual(result.wem_count, 0)

    def test_missing_bkhd_raises(self):
        raw = b"XYZW" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        with tempfile.TemporaryDirectory() as tmpdir:
            bnk = Path(tmpdir) / "bad.bnk"
            bnk.write_bytes(raw)
            with self.assertRaises(WwiseFormatError):
                extract_bank(bnk, Path(tmpdir) / "out")


class ExtractAllTests(unittest.TestCase):
    def test_finds_and_extracts_banks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            source.mkdir()
            bank_data = _make_bank(wems=[b"\xaa" * 16])
            (source / "music.bnk").write_bytes(bank_data)
            out = Path(tmpdir) / "out"
            result = extract_all(source, out)
            self.assertEqual(result.banks_found, 1)
            self.assertEqual(result.banks_extracted, 1)
            self.assertEqual(result.total_wems, 1)
            self.assertTrue((out / "music" / "1000.wem").is_file())

    def test_report_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            source.mkdir()
            out = Path(tmpdir) / "out"
            result = extract_all(source, out)
            self.assertEqual(result.banks_found, 0)
            import json
            report = json.loads((out / "wwise_report.json").read_text())
            self.assertEqual(report["banks_found"], 0)


if __name__ == "__main__":
    unittest.main()
