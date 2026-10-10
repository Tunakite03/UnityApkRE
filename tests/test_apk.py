import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fixtures import write_zip
from uare.apk import ArchiveRole, classify_archive, extract_apk_set
from uare.errors import ApkFormatError, OutputDirectoryError


class ClassifyTests(unittest.TestCase):
    def test_roles(self) -> None:
        cases = {
            "base.apk": ArchiveRole("base", None),
            "split_config.arm64_v8a.apk": ArchiveRole("config.abi", "arm64-v8a"),
            "split_config.xxhdpi.apk": ArchiveRole("config.density", "xxhdpi"),
            "split_config.vi.apk": ArchiveRole("config.locale", "vi"),
            "split_base_assets.apk": ArchiveRole("split", "base_assets"),
            "main.12.com.x.obb": ArchiveRole("obb", "main"),
            "game.apk": ArchiveRole("standalone", None),
        }
        for name, role in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify_archive(Path(name)), role)

    def test_rejects_other_extensions(self) -> None:
        with self.assertRaises(ApkFormatError):
            classify_archive(Path("game.zip"))


class ExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_merges_split_parts_across_archives(self) -> None:
        base = write_zip(self.tmp / "in/base.apk", {
            "AndroidManifest.xml": b"axml",
            "classes.dex": b"dex",
            "assets/bin/Data/level0.split0": b"AAAA",
            "assets/bin/Data/globalgamemanagers": b"ggm",
        })
        assets = write_zip(self.tmp / "in/split_base_assets.apk", {
            "assets/bin/Data/level0.split2": b"CC",
            "assets/bin/Data/level0.split1": b"BBB",
            "assets/bin/Data/globalgamemanagers": b"ggm",
            "assets/aa/catalog.json": b"{}",
        })
        abi = write_zip(self.tmp / "in/split_config.arm64_v8a.apk", {"lib/arm64-v8a/libil2cpp.so": b"\x7fELF"})

        out = self.tmp / "out"
        result = extract_apk_set([base, assets, abi], out)

        self.assertEqual((out / "Data/level0").read_bytes(), b"AAAABBBCC")
        self.assertEqual((out / "Data/globalgamemanagers").read_bytes(), b"ggm")
        self.assertEqual((out / "lib/arm64-v8a/libil2cpp.so").read_bytes(), b"\x7fELF")
        self.assertEqual((out / "assets/aa/catalog.json").read_bytes(), b"{}")
        self.assertEqual((out / "archives/base/AndroidManifest.xml").read_bytes(), b"axml")
        self.assertEqual(result.merged_split_files, {"Data/level0": 3})
        self.assertEqual(result.duplicate_entries_skipped, 1)
        self.assertEqual(result.abis, ("arm64-v8a",))
        self.assertEqual(json.loads((out / "extraction.json").read_text())["files_written"], result.files_written)

    def test_conflicting_duplicates_fail(self) -> None:
        a = write_zip(self.tmp / "in/base.apk", {"assets/bin/Data/level1": b"one"})
        b = write_zip(self.tmp / "in/split_x.apk", {"assets/bin/Data/level1": b"two"})
        with self.assertRaises(ApkFormatError):
            extract_apk_set([a, b], self.tmp / "out")

    def test_missing_split_part_fails(self) -> None:
        a = write_zip(self.tmp / "in/base.apk", {
            "assets/bin/Data/level0.split0": b"A",
            "assets/bin/Data/level0.split2": b"C",
        })
        with self.assertRaises(ApkFormatError):
            extract_apk_set([a], self.tmp / "out")

    def test_zip_slip_is_rejected(self) -> None:
        path = self.tmp / "in/base.apk"
        path.parent.mkdir(parents=True)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("assets/../../evil.txt", b"x")
        with self.assertRaises(ApkFormatError):
            extract_apk_set([path], self.tmp / "out")
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_windows_drive_and_dot_entries_are_rejected(self) -> None:
        for name in ("C:/Users/x/evil", "D:foo", ".", "a/.."):
            with self.subTest(name=name):
                path = self.tmp / f"in_{abs(hash(name))}/base.apk"
                path.parent.mkdir(parents=True)
                with zipfile.ZipFile(path, "w") as zf:
                    zf.writestr(name, b"x")
                with self.assertRaises(ApkFormatError):
                    extract_apk_set([path], self.tmp / f"out_{abs(hash(name))}")

    def test_case_only_collision_fails(self) -> None:
        a = write_zip(self.tmp / "in/base.apk", {"assets/Config.json": b"1", "assets/config.json": b"2"})
        with self.assertRaises(ApkFormatError):
            extract_apk_set([a], self.tmp / "out")

    def test_file_directory_collision_fails(self) -> None:
        a = write_zip(self.tmp / "in/base.apk", {"assets/foo": b"1", "assets/foo/bar": b"2"})
        with self.assertRaises(ApkFormatError):
            extract_apk_set([a], self.tmp / "out")

    def test_dot_stem_archive_is_rejected(self) -> None:
        a = write_zip(self.tmp / "in/...apk", {"assets/x": b"1"})
        with self.assertRaises(ApkFormatError):
            extract_apk_set([a], self.tmp / "out")

    def test_corrupt_entry_fails_without_partial_output(self) -> None:
        path = write_zip(self.tmp / "in/base.apk", {"assets/bin/Data/level0": b"A" * 4096})
        raw = bytearray(path.read_bytes())
        header = raw.find(b"PK\x03\x04")
        name_len = int.from_bytes(raw[header + 26:header + 28], "little")
        extra_len = int.from_bytes(raw[header + 28:header + 30], "little")
        data_at = header + 30 + name_len + extra_len
        raw[data_at + 2] ^= 0xFF
        path.write_bytes(bytes(raw))
        with self.assertRaises(ApkFormatError):
            extract_apk_set([path], self.tmp / "out")
        self.assertFalse((self.tmp / "out").exists())
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["in"])

    def test_non_empty_output_is_rejected(self) -> None:
        a = write_zip(self.tmp / "in/base.apk", {"assets/bin/Data/level0": b"A"})
        (self.tmp / "out").mkdir()
        (self.tmp / "out/keep.txt").write_text("x")
        with self.assertRaises(OutputDirectoryError):
            extract_apk_set([a], self.tmp / "out")

    def test_corrupt_archive_fails(self) -> None:
        bad = self.tmp / "base.apk"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(ApkFormatError):
            extract_apk_set([bad], self.tmp / "out")


if __name__ == "__main__":
    unittest.main()
