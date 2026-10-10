import os
import tempfile
import unittest
from pathlib import Path

from fixtures import elf64, il2cpp_metadata, serialized_v17, serialized_v22, unityfs_bundle
from uare.elf import inspect_elf
from uare.errors import ElfFormatError, Il2CppMetadataError, UnityFormatError
from uare.il2cpp import parse_identifier_table, parse_literal_table, parse_metadata_header, read_metadata
from uare.unity import (
    FileKind,
    detect_kind,
    parse_bundle_header,
    parse_serialized_header,
    read_serialized_header,
)


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path


class SerializedFileTests(TempDirCase):
    def test_format_22_header(self) -> None:
        path = self.write("level4", serialized_v22("6000.2.15f1", 13, False, 383, 8192))
        self.assertIs(detect_kind(path), FileKind.SERIALIZED)
        header = read_serialized_header(path)
        self.assertEqual(header.format_version, 22)
        self.assertEqual(header.unity_version, "6000.2.15f1")
        self.assertEqual(header.target_platform_name, "Android")
        self.assertFalse(header.type_tree_enabled)
        self.assertEqual(header.type_count, 383)
        self.assertEqual(header.file_size, 8192)

    def test_format_17_header(self) -> None:
        path = self.write("sharedassets0.assets", serialized_v17("2019.4.40f1", 9, 12, 4096))
        header = read_serialized_header(path)
        self.assertEqual((header.format_version, header.unity_version), (17, "2019.4.40f1"))
        self.assertEqual(header.target_platform_name, "iOS")
        self.assertTrue(header.type_tree_enabled)

    def test_size_mismatch_is_rejected(self) -> None:
        data = serialized_v22("6000.2.15f1", 13, False, 1, 4096)
        with self.assertRaises(UnityFormatError):
            parse_serialized_header(data, 4097)
        self.assertIs(detect_kind(self.write("truncated", data[:4000])), FileKind.UNKNOWN)

    def test_resource_and_text_kinds(self) -> None:
        self.assertIs(detect_kind(self.write("sharedassets0.assets.resS", b"\x00" * 64)), FileKind.RESOURCE)
        self.assertIs(detect_kind(self.write("boot.config", b"gfx-enable-jobs=1\n")), FileKind.TEXT)


class BundleTests(unittest.TestCase):
    def test_unityfs_lz4(self) -> None:
        header = parse_bundle_header(unityfs_bundle(flags=0x43))
        self.assertEqual(header.engine_version, "2021.3.45f1")
        self.assertEqual(header.compression, "lz4hc")
        self.assertEqual(header.total_size, 1234)

    def test_rejects_unknown_signature(self) -> None:
        with self.assertRaises(UnityFormatError):
            parse_bundle_header(b"NotUnity\x00" + b"\x00" * 64)


class Il2CppTests(TempDirCase):
    HEAP = b"Assembly-CSharp\x00MainLobbyPage\x00\x00\x80\xa0\x00OnClick\x00"

    def test_reads_identifiers_and_literals(self) -> None:
        path = self.write("global-metadata.dat", il2cpp_metadata(29, [b"hello", "xin chào".encode()], self.HEAP))
        header, identifiers, literals = read_metadata(path)
        self.assertEqual(header.version, 29)
        self.assertEqual([text for _, text in identifiers.entries], ["Assembly-CSharp", "MainLobbyPage", "OnClick"])
        self.assertEqual(identifiers.entries[1][0], len("Assembly-CSharp") + 1)
        self.assertEqual(identifiers.non_text_entries, 1)
        self.assertEqual(literals.literals, ("hello", "xin chào"))

    def test_wrong_magic_reports_encryption(self) -> None:
        data = bytearray(il2cpp_metadata(29, [], self.HEAP))
        data[0:4] = b"\x12\x34\x56\x78"
        with self.assertRaisesRegex(Il2CppMetadataError, "encrypted"):
            parse_metadata_header(bytes(data), len(data))

    def test_section_out_of_bounds(self) -> None:
        data = il2cpp_metadata(29, [b"x"], self.HEAP)
        with self.assertRaises(Il2CppMetadataError):
            parse_metadata_header(data, len(data) - 1)

    def test_literal_out_of_bounds(self) -> None:
        with self.assertRaises(Il2CppMetadataError):
            parse_literal_table(b"\x10\x00\x00\x00\x00\x00\x00\x00", b"short")

    def test_literals_overlapping_whole_blob_are_rejected(self) -> None:
        entry = (5).to_bytes(4, "little") + (0).to_bytes(4, "little")
        with self.assertRaisesRegex(Il2CppMetadataError, "inconsistent"):
            parse_literal_table(entry * 4, b"hello")

    def test_identifier_offsets_follow_heap(self) -> None:
        table = parse_identifier_table(b"\x00A\x00BC\x00")
        self.assertEqual(table.entries, ((1, "A"), (3, "BC")))


class ElfTests(TempDirCase):
    def test_random_exec_segment_is_flagged(self) -> None:
        path = self.write("libil2cpp.so", elf64(os.urandom(2 << 20), ["il2cpp_init", "JNI_OnLoad", "il2cpp_domain_get"],
                                                ["malloc"]))
        report = inspect_elf(path)
        self.assertEqual(report.machine, "aarch64")
        self.assertEqual(report.packing_verdict, "likely_packed_or_encrypted")
        self.assertEqual(report.symbols.il2cpp_exports, ("il2cpp_domain_get", "il2cpp_init"))
        self.assertEqual((report.symbols.defined, report.symbols.imported), (3, 1))
        self.assertEqual(report.symbols.section_table, "ok")
        self.assertEqual(report.load_segments[0].flags, "R-X")

    def test_imported_il2cpp_symbols_are_not_exports(self) -> None:
        report = inspect_elf(self.write("libunity.so", elf64(bytes(range(64)) * 1024, ["UnitySendMessage"],
                                                             ["il2cpp_init", "il2cpp_domain_get"])))
        self.assertEqual(report.symbols.il2cpp_exports, ())
        self.assertEqual(report.symbols.imported, 2)

    def test_structured_code_is_normal(self) -> None:
        code = bytes(range(0, 64)) * (1 << 14)
        report = inspect_elf(self.write("libmain.so", elf64(code, ["JNI_OnLoad"], [])))
        self.assertEqual(report.packing_verdict, "normal")

    def test_filler_windows_are_counted(self) -> None:
        report = inspect_elf(self.write("libfill.so", elf64(b"AGAG" * (1 << 19), [], [])))
        self.assertEqual(report.fill_windows, report.exec_windows)

    def test_short_tail_window_does_not_dilute_verdict(self) -> None:
        report = inspect_elf(self.write("libtail.so", elf64(os.urandom((1 << 20) + 4096), [], [])))
        self.assertEqual((report.exec_windows, report.high_entropy_windows), (1, 1))

    def test_section_table_out_of_bounds_is_reported(self) -> None:
        data = bytearray(elf64(bytes(range(64)) * 64, ["x"], []))
        data[0x28:0x30] = (len(data) * 4).to_bytes(8, "little")  # e_shoff past EOF
        report = inspect_elf(self.write("libstripped.so", bytes(data)))
        self.assertEqual(report.symbols.section_table, "out_of_bounds")
        self.assertIsNone(report.symbols.defined)

    def test_not_elf(self) -> None:
        with self.assertRaises(ElfFormatError):
            inspect_elf(self.write("bad.so", b"MZ" + b"\x00" * 100))


if __name__ == "__main__":
    unittest.main()
