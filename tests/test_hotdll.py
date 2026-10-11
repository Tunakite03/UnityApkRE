"""Tests for uare.hotdll: key recovery from metadata, scheme search, and decryption."""

import hashlib
import json
import struct
import unittest
from pathlib import Path

from tests.fixtures import METADATA_MAGIC, write_zip

from uare.errors import UareError
from uare.hotdll import (
    SCHEMES,
    Candidate,
    DecryptResult,
    Solution,
    _StdlibAES,
    _chacha20_block,
    _common_prefix,
    _crypto,
    _stdlib_backend,
    _stdlib_rc4,
    decrypt_directory,
    extract_key_candidates,
    find_solution,
    summarize,
)

# --------------------------------------------------------------------- metadata fixture builder

_TYPE_STRIDE, _FIELD_STRIDE = 88, 12
_HEADER_PAIRS = 31
_SECS = {"string": 2, "fdv": 7, "blob": 8, "fields": 11, "types": 19}


def build_metadata(arrays: list[bytes]) -> bytes:
    """A metadata v31 file whose only type is a <PrivateImplementationDetails> class holding
    `arrays` as SHA-256-named fields with fieldDefaultValues entries into the blob."""
    heap = bytearray(b"\x00")
    fields = bytearray()
    fdv = bytearray()
    blob = bytearray()

    def intern(text: str) -> int:
        end = heap.find(b"\x00", 1)
        if text.encode() in bytes(heap):
            return bytes(heap).index(text.encode())
        off = len(heap)
        heap.extend(text.encode() + b"\x00")
        return off

    intern("<PrivateImplementationDetails>")
    for index, data in enumerate(arrays):
        sha = hashlib.sha256(data).hexdigest().upper()
        fields += struct.pack("<i", intern(sha))
        fields += struct.pack("<i", 0) * 2  # typeIndex, token
        fdv += struct.pack("<iii", index, 0, len(blob))
        blob += data

    pairs = [(8 + _HEADER_PAIRS * 8, 0)] * _HEADER_PAIRS
    body = bytearray()

    def place(index: int, chunk: bytes) -> None:
        pairs[index] = (8 + _HEADER_PAIRS * 8 + len(body), len(chunk))
        body.extend(chunk)

    place(_SECS["string"], bytes(heap))
    place(_SECS["fdv"], bytes(fdv))
    place(_SECS["blob"], bytes(blob))
    place(_SECS["fields"], bytes(fields))
    type_record = bytearray(_TYPE_STRIDE)
    struct.pack_into("<i", type_record, 0, intern("<PrivateImplementationDetails>"))
    struct.pack_into("<i", type_record, 32, 0)   # fieldStart
    struct.pack_into("<H", type_record, 68, len(arrays))  # field_count
    place(_SECS["types"], bytes(type_record))

    header = struct.pack("<Ii", METADATA_MAGIC, 31)
    for offset, size in pairs:
        header += struct.pack("<II", offset, size)
    return header + bytes(body)


def _tiny_pe() -> bytes:
    """A minimal but structurally valid PE32 used as a decryption target."""
    lfanew = 0x80
    pe = bytearray(lfanew)
    pe[0:2] = b"MZ"
    struct.pack_into("<I", pe, 0x3C, lfanew)
    coff = struct.pack("<HHIIIHH", 0x14C, 1, 0, 0, 0, 0xE0, 0x0102)
    pe += b"PE\x00\x00" + coff
    pe += b"\x00" * (0x1A0 - len(pe))
    return bytes(pe)


def _pkcs7(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Encrypt with the stdlib AES so tests do not need pycryptodome."""
    rks = _StdlibAES.expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[off:off + 16], prev))
        enc = _StdlibAES.encrypt_block(block, rks)
        out += enc
        prev = enc
    return bytes(out)


class AesCorrectnessTest(unittest.TestCase):
    """The stdlib AES fallback must agree with itself across directions and known vectors."""

    # FIPS-197 appendix C.1 (AES-128): plaintext, key, ciphertext
    def test_known_vector(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        rks = _StdlibAES.expand_key(key)
        self.assertEqual(_StdlibAES.encrypt_block(plain, rks), cipher)
        self.assertEqual(_StdlibAES.decrypt_block(cipher, rks), plain)

    def test_cbc_roundtrip(self):
        key, iv = bytes(range(16)), bytes(range(16, 32))
        data = _tiny_pe() * 4
        aes = _stdlib_backend()
        self.assertEqual(aes.aes_cbc(key, iv, _aes_cbc_encrypt(key, iv, data)), data)

    def test_rc4_selfinverse(self):
        key = bytes(range(16))
        data = _tiny_pe()
        self.assertEqual(_stdlib_rc4(key, _stdlib_rc4(key, data)), data)

    def test_chacha20_rfc7539_vector(self):
        # RFC 7539 section 2.3.2: key = 0x00..0x1f, counter = 1, nonce = 000000090000004a00000000
        key = bytes(range(32))
        nonce = bytes.fromhex("000000090000004a00000000")
        block = _chacha20_block(key, struct.pack("<I", 1) + nonce)
        self.assertEqual(block[:16].hex(), "10f1e7e4d13b5915500fdd1fa32071c4")


class CandidateExtractionTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.metadata = self.tmp / "global-metadata.dat"
        self.key = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.iv = bytes.fromhex("aabbccddeeff00112233445566778899")
        self.magic = bytes.fromhex("21f7ddab31690c08")
        arrays = [self.key, self.iv, self.magic, b"decoy-not-a-key!!"]
        self.metadata.write_bytes(build_metadata(arrays))

    def test_extracts_sha_verified_candidates(self):
        candidates = extract_key_candidates(self.metadata)
        self.assertEqual(len(candidates), 4)
        data = {c.data for c in candidates}
        self.assertIn(self.key, data)
        self.assertIn(self.magic, data)
        for c in candidates:
            self.assertEqual(hashlib.sha256(c.data).hexdigest().upper(), c.name.upper())

    def test_rejects_wrong_version(self):
        blob = bytearray(self.metadata.read_bytes())
        struct.pack_into("<i", blob, 4, 24)
        self.metadata.write_bytes(bytes(blob))
        with self.assertRaises(UareError):
            extract_key_candidates(self.metadata)

    def test_rejects_bad_magic(self):
        blob = bytearray(self.metadata.read_bytes())
        blob[0:4] = b"\x00\x00\x00\x00"
        self.metadata.write_bytes(bytes(blob))
        with self.assertRaises(UareError):
            extract_key_candidates(self.metadata)


class FindSolutionTest(unittest.TestCase):
    def setUp(self):
        self.key = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.iv = bytes.fromhex("aabbccddeeff00112233445566778899")
        self.magic = bytes.fromhex("21f7ddab31690c08")
        self.pe = _tiny_pe()
        self.decoys = [bytes(range(16)), bytes(range(12)), b"x" * 32]
        self.candidates = [Candidate(0, i, hashlib.sha256(v).hexdigest().upper(), len(v), v)
                           for i, v in enumerate([self.key, self.iv, self.magic, *self.decoys])]

    def _encrypted(self) -> bytes:
        return self.magic + _aes_cbc_encrypt(self.key, self.iv, _pkcs7(self.pe))

    def test_finds_aes_cbc(self):
        for backend in (_stdlib_backend(), _crypto()):
            with self.subTest(backend=type(backend).__name__):
                solution = find_solution([self._encrypted()], self.candidates, backend)
                self.assertEqual(solution.scheme, "aes-128-cbc")
                self.assertEqual(solution.key, self.key)
                self.assertEqual(solution.nonce, self.iv)
                self.assertEqual(solution.magic, self.magic)

    def test_rejects_noise(self):
        with self.assertRaises(UareError):
            find_solution([b"\x00" * 512], self.candidates, _stdlib_backend())

    def test_common_prefix(self):
        self.assertEqual(_common_prefix([b"abcdef", b"abcxyz"]), b"abc")
        self.assertEqual(_common_prefix([b"abc"]), b"abc")
        self.assertEqual(_common_prefix([]), b"")


class DecryptDirectoryTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.dll_dir = self.tmp / "hotdll"
        self.dll_dir.mkdir()
        self.out = self.tmp / "out"
        self.key = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.iv = bytes.fromhex("aabbccddeeff00112233445566778899")
        self.magic = bytes.fromhex("21f7ddab31690c08")
        self.metadata = self.tmp / "global-metadata.dat"
        self.metadata.write_bytes(build_metadata([self.key, self.iv, self.magic]))
        # encrypted sample
        enc = self.magic + _aes_cbc_encrypt(self.key, self.iv, _pkcs7(_tiny_pe()))
        (self.dll_dir / "Hotfix.dll").write_bytes(enc)
        (self.dll_dir / "Plain.dll").write_bytes(_tiny_pe())  # already plain

    def test_end_to_end_with_key_search(self):
        result = decrypt_directory(self.dll_dir, self.out, metadata=self.metadata)
        self.assertTrue(self.out.is_dir())
        plain = (self.out / "decrypted" / "Hotfix.dll").read_bytes()
        self.assertEqual(plain, _tiny_pe())
        self.assertIn("Plain.dll", result.skipped)
        keys = json.loads((self.out / "keys.json").read_text())
        self.assertEqual(keys["algo"], "aes-128-cbc")
        self.assertEqual(keys["key"], self.key.hex())
        report = json.loads((self.out / "report.json").read_text())
        self.assertEqual(report["files"][0]["status"], "decrypted")

    def test_outdir_must_not_exist(self):
        self.out.mkdir()
        (self.out / "stale.txt").write_text("x")
        with self.assertRaises(UareError):
            decrypt_directory(self.dll_dir, self.out, metadata=self.metadata)

    def test_explicit_keys_skip_search(self):
        result = decrypt_directory(self.dll_dir, self.out, algo="aes-128-cbc",
                                   key=self.key, iv=self.iv, magic=self.magic)
        self.assertEqual((self.out / "decrypted" / "Hotfix.dll").read_bytes(), _tiny_pe())

    def test_keys_file_roundtrip(self):
        decrypt_directory(self.dll_dir, self.out, metadata=self.metadata)
        keys = self.out / "keys.json"
        out2 = self.tmp / "out2"
        decrypt_directory(self.dll_dir, out2, keys_file=keys)
        self.assertEqual((out2 / "decrypted" / "Hotfix.dll").read_bytes(), _tiny_pe())

    def test_missing_keys_reported(self):
        (self.dll_dir / "Hotfix.dll").write_bytes(b"\x21\xf7\xdd\xab" + b"\x00" * 300)
        with self.assertRaises(UareError):
            decrypt_directory(self.dll_dir, self.out, metadata=self.metadata)

    def test_summarize(self):
        result = decrypt_directory(self.dll_dir, self.out, metadata=self.metadata)
        text = "\n".join(summarize(result))
        self.assertIn("aes-128-cbc", text)
        self.assertIn("decrypted        : 1/1", text)

    def test_no_metadata_found(self):
        (self.metadata).unlink()
        with self.assertRaises(UareError):
            decrypt_directory(self.dll_dir, self.out)


if __name__ == "__main__":
    unittest.main()
