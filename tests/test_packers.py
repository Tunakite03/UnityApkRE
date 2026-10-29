import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fixtures import elf64
from uare.cli import main
from uare.elf import inspect_elf
from uare.packers import identify, identify_file


def _packed(marker: bytes) -> bytes:
    return elf64(os.urandom(1 << 20), ["il2cpp_init"], []) + marker


def _clean() -> bytes:
    return elf64(bytes(range(64)) * 1024, ["il2cpp_init"], [])


class PackerIdentifyTests(unittest.TestCase):
    def test_marker_is_identified(self) -> None:
        match = identify(_packed(b"libmobisec"))
        self.assertIsNotNone(match)
        self.assertEqual(match.name, "SecNeo (mobisec)")

    def test_no_marker_returns_none(self) -> None:
        self.assertIsNone(identify(_clean()))

    def test_elf_report_carries_the_packer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "libil2cpp.so"
            path.write_bytes(_packed(b"libjiagu"))
            report = inspect_elf(path)
        self.assertEqual(report.packer, "360 Jiagu")
        self.assertEqual(report.packing_verdict, "likely_packed_or_encrypted")


class UnpackNativeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reports_packer_without_a_command(self) -> None:
        so = self.tmp / "libil2cpp.so"
        so.write_bytes(_packed(b"libDexHelper"))
        self.assertEqual(main(["unpack-native", str(so), "--out", str(self.tmp / "out")]), 0)
        self.assertFalse((self.tmp / "out").exists())

    def test_external_unpacker_produces_a_clean_elf(self) -> None:
        so = self.tmp / "libil2cpp.so"
        so.write_bytes(_packed(b"libjiagu"))
        clean = self.tmp / "clean.so"
        clean.write_bytes(_clean())
        code = f"import sys,shutil;shutil.copyfile({str(clean)!r}, sys.argv[1])"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} {{output}}"

        rc = main(["unpack-native", str(so), "--out", str(self.tmp / "out"), "--cmd", command])
        self.assertEqual(rc, 0)
        report = json.loads((self.tmp / "out/elf_report.json").read_text())
        self.assertEqual(report["packing_verdict"], "normal")
        self.assertEqual((self.tmp / "out/libil2cpp.so").read_bytes(), _clean())

    def test_unpacker_that_still_leaves_it_packed_exits_1(self) -> None:
        so = self.tmp / "libil2cpp.so"
        so.write_bytes(_packed(b"libjiagu"))
        packed = self.tmp / "still.so"
        packed.write_bytes(elf64(os.urandom(1 << 20), [], []))
        code = f"import sys,shutil;shutil.copyfile({str(packed)!r}, sys.argv[1])"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} {{output}}"
        self.assertEqual(main(["unpack-native", str(so), "--out", str(self.tmp / "out"), "--cmd", command]), 1)

    def test_unpacker_that_hangs_is_killed_at_the_timeout_and_leaves_no_output(self) -> None:
        so = self.tmp / "libil2cpp.so"
        so.write_bytes(_packed(b"libjiagu"))
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(30)')} {{output}}"
        started = time.monotonic()
        rc = main(["unpack-native", str(so), "--out", str(self.tmp / "out"), "--cmd", command, "--cmd-timeout", "0.5"])
        self.assertEqual(rc, 2)
        self.assertLess(time.monotonic() - started, 20)
        self.assertFalse((self.tmp / "out").exists())

    def test_identify_file_reads_from_disk(self) -> None:
        so = self.tmp / "libil2cpp.so"
        so.write_bytes(_packed(b"libnsa"))
        self.assertEqual(identify_file(so).name, "Nagain")


if __name__ == "__main__":
    unittest.main()
