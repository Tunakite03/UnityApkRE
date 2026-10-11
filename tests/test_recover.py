import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import il2cpp_metadata
from uare.errors import Il2CppMetadataError
from uare.recover import header_version, recover, recover_file


def _xored(key: bytes, plain: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(plain))


class RecoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_identity_when_magic_is_already_valid(self) -> None:
        plain = il2cpp_metadata(31, [b"Play"], b"Assembly-CSharp\x00")
        result = recover(plain)
        self.assertEqual(result.scheme, "identity")
        self.assertEqual(result.data, plain)
        self.assertEqual(header_version(plain), 31)

    def test_single_byte_xor_is_recovered_automatically(self) -> None:
        plain = il2cpp_metadata(31, [b"Play"], b"Assembly-CSharp\x00")
        result = recover(_xored(b"\x5a", plain))
        self.assertEqual(result.scheme, "xor-single-byte")
        self.assertEqual(result.data, plain)

    def test_short_repeating_xor_is_recovered_automatically(self) -> None:
        plain = il2cpp_metadata(31, [b"Play"], b"Assembly-CSharp\x00")
        result = recover(_xored(b"\x12\x34", plain))
        self.assertEqual(result.scheme, "xor-repeating")
        self.assertEqual(result.data, plain)

    def test_explicit_key(self) -> None:
        plain = il2cpp_metadata(31, [b"Play"], b"Assembly-CSharp\x00")
        result = recover(_xored(b"\x01\x02\x03\x04\x05", plain), key=b"\x01\x02\x03\x04\x05")
        self.assertEqual(result.scheme, "xor-key")
        self.assertEqual(result.data, plain)

    def test_external_command_hook(self) -> None:
        plain = il2cpp_metadata(31, [b"Play"], b"Assembly-CSharp\x00")
        code = "import sys;d=open(sys.argv[1],'rb').read();open(sys.argv[2],'wb').write(bytes(b^0x7f for b in d))"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} {{input}} {{output}}"
        result = recover(_xored(b"\x7f", plain), command=command)
        self.assertEqual(result.scheme, "external")
        self.assertEqual(result.data, plain)

    def test_unknown_wrapper_raises_not_guesses(self) -> None:
        with self.assertRaises(Il2CppMetadataError):
            recover(b"\xde\xad\xbe\xef" * 64)

    def test_recover_file_and_cli(self) -> None:
        from uare.cli import main
        data = self.tmp / "Data"
        (data / "Managed/Metadata").mkdir(parents=True)
        (data / "globalgamemanagers").write_bytes(b"gg")
        plain = il2cpp_metadata(31, [b"Play"], b"Assembly-CSharp\x00")
        (data / "Managed/Metadata/global-metadata.dat").write_bytes(_xored(b"\x5a", plain))

        rc = main(["recover-metadata", str(data), "--out", str(self.tmp / "rec")])
        self.assertEqual(rc, 0)
        recovered = (self.tmp / "rec/global-metadata.dat").read_bytes()
        self.assertEqual(recovered, plain)
        meta = json.loads((self.tmp / "rec/recovery.json").read_text())
        self.assertEqual(meta["scheme"], "xor-single-byte")
        self.assertEqual(meta["version"], 31)

    def test_recover_file_reports_missing(self) -> None:
        with self.assertRaises(Il2CppMetadataError):
            recover_file(self.tmp / "nope.dat")


if __name__ == "__main__":
    unittest.main()
