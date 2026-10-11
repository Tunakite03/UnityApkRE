"""Decrypt HybridCLR-style encrypted hot-update assemblies (opt-in deobfuscation).

Some Unity games ship their HybridCLR hot-update DLLs as encrypted blobs inside AssetBundles:
the file is `MAGIC || ciphertext`, and the keys live in the AOT build as `new byte[]{...}`
array initializers. The C# compiler parks those literals in compiler-generated
`<PrivateImplementationDetails>` classes, whose fields are named after the SHA-256 of their
own bytes and whose values sit in the `fieldAndParameterDefaultValueData` blob of
`global-metadata.dat`, pointed at by the `fieldDefaultValues` table.

So the keys never touch the .so: this module recovers every such array straight from the
metadata (SHA-256 verified against the field name, never guessed), then tries the standard
HybridCLR.Security schemes (AES-128-CBC, AES-128-GCM, ChaCha20, RC4, XOR) with the common
magic lengths until one decrypts a file to a valid PE (.NET hot-update assemblies).
A candidate is accepted only when the whole sample decrypts to a real PE with a consistent
PKCS#7 tail for block ciphers; anything else is reported as not found, never guessed.

Requires metadata v27-v31 (fieldDefaultValues section layout). AES/RC4/ChaCha run on a
pure-Python fallback when neither pycryptodome nor cryptography is installed.
"""

import hashlib
import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path

from uare.errors import Il2CppMetadataError, UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.hotdll")

# metadata section indices (stable across v27-v31)
_SEC_STRING, _SEC_FDV, _SEC_BLOB, _SEC_FIELDS, _SEC_TYPES = 2, 7, 8, 11, 19
_TYPE_STRIDE, _FIELD_STRIDE, _FDV_STRIDE = 88, 12, 12
_SUPPORTED_VERSIONS = (27, 28, 29, 30, 31)
_MAX_TYPE_COUNT = 200_000
_MAX_KEY_SIZE = 64  # HybridCLR keys are <= 32; a little slack for verification

_MAGIC_LENGTHS = (8, 16)  # DllEncryptor magic fields are short; both lengths are tried
SCHEMES = ("aes-128-cbc", "aes-128-gcm", "chacha20", "rc4", "xor")


class HotDllError(UareError):
    """The hot-update assemblies could not be located, or their keys were not found."""


# --------------------------------------------------------------------------- pure crypto fallback

class _StdlibAES:
    """AES-128 block primitives (both directions) for the stdlib fallback. Correct, not fast."""

    @staticmethod
    def _compute_sbox() -> bytes:
        # multiplicative inverse in GF(2^8) followed by the affine transform; equal to the
        # canonical AES table and cheap to derive once at import.
        def gmul(a: int, b: int) -> int:
            p = 0
            for _ in range(8):
                if b & 1:
                    p ^= a
                hi = a & 0x80
                a = (a << 1) & 0xFF
                if hi:
                    a ^= 0x1B
                b >>= 1
            return p

        table = bytearray(256)
        for b in range(256):
            if b == 0:
                inv = 0
            else:
                inv = next(i for i in range(1, 256) if gmul(b, i) == 1)
            value = inv
            rotated = inv
            for _ in range(4):
                rotated = ((rotated << 1) | (rotated >> 7)) & 0xFF
                value ^= rotated
            table[b] = value ^ 0x63
        return bytes(table)

    SBOX = _compute_sbox()
    INV_SBOX = bytearray(256)
    for _i, _v in enumerate(SBOX):
        INV_SBOX[_v] = _i
    INV_SBOX = bytes(INV_SBOX)

    @classmethod
    def expand_key(cls, key: bytes) -> list[bytes]:
        words = [list(key[4 * i:4 * i + 4]) for i in range(4)]
        rcon = 1
        for i in range(4, 44):
            tmp = list(words[i - 1])
            if i % 4 == 0:
                tmp = tmp[1:] + tmp[:1]
                tmp = [cls.SBOX[b] for b in tmp]
                tmp[0] ^= rcon
                rcon = ((rcon << 1) ^ 0x1B) & 0xFF if rcon & 0x80 else (rcon << 1)
            words.append([a ^ b for a, b in zip(words[i - 4], tmp)])
        return [b"".join(bytes(w) for w in words[4 * r:4 * r + 4]) for r in range(11)]

    @staticmethod
    def _gmul(a: int, b: int) -> int:
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            hi = a & 0x80
            a = (a << 1) & 0xFF
            if hi:
                a ^= 0x1B
            b >>= 1
        return p

    @classmethod
    def decrypt_block(cls, block: bytes, rks: list[bytes]) -> bytes:
        s = [block[i] ^ rks[10][i] for i in range(16)]

        def inv_shift_rows(s):
            return [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
                    s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]

        def inv_sub(s):
            return [cls.INV_SBOX[b] for b in s]

        def inv_mix(s):
            out = []
            for c in range(4):
                col = s[4 * c:4 * c + 4]
                out.extend([
                    cls._gmul(col[0], 14) ^ cls._gmul(col[1], 11) ^ cls._gmul(col[2], 13) ^ cls._gmul(col[3], 9),
                    cls._gmul(col[0], 9) ^ cls._gmul(col[1], 14) ^ cls._gmul(col[2], 11) ^ cls._gmul(col[3], 13),
                    cls._gmul(col[0], 13) ^ cls._gmul(col[1], 9) ^ cls._gmul(col[2], 14) ^ cls._gmul(col[3], 11),
                    cls._gmul(col[0], 11) ^ cls._gmul(col[1], 13) ^ cls._gmul(col[2], 9) ^ cls._gmul(col[3], 14),
                ])
            return out

        for rnd in range(9, 0, -1):
            s = inv_sub(inv_shift_rows(s))
            s = [s[i] ^ rks[rnd][i] for i in range(16)]
            s = inv_mix(s)
        s = inv_sub(inv_shift_rows(s))
        s = [s[i] ^ rks[0][i] for i in range(16)]
        return bytes(s)

    @classmethod
    def encrypt_block(cls, block: bytes, rks: list[bytes]) -> bytes:
        s = [block[i] ^ rks[0][i] for i in range(16)]

        def shift_rows(s):
            return [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
                    s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]

        def mix(s):
            out = []
            for c in range(4):
                col = s[4 * c:4 * c + 4]
                out.extend([
                    cls._gmul(col[0], 2) ^ cls._gmul(col[1], 3) ^ col[2] ^ col[3],
                    col[0] ^ cls._gmul(col[1], 2) ^ cls._gmul(col[2], 3) ^ col[3],
                    col[0] ^ col[1] ^ cls._gmul(col[2], 2) ^ cls._gmul(col[3], 3),
                    cls._gmul(col[0], 3) ^ col[1] ^ col[2] ^ cls._gmul(col[3], 2),
                ])
            return out

        for rnd in range(1, 10):
            s = [cls.SBOX[b] for b in s]
            s = shift_rows(s)
            s = mix(s)
            s = [s[i] ^ rks[rnd][i] for i in range(16)]
        s = [cls.SBOX[b] for b in s]
        s = shift_rows(s)
        s = [s[i] ^ rks[10][i] for i in range(16)]
        return bytes(s)

    @classmethod
    def cbc_decrypt(cls, key: bytes, iv: bytes, data: bytes) -> bytes:
        rks = cls.expand_key(key)
        out = bytearray()
        prev = iv
        for off in range(0, len(data), 16):
            block = data[off:off + 16]
            out += bytes(a ^ b for a, b in zip(cls.decrypt_block(block, rks), prev))
            prev = block
        return bytes(out)

    @classmethod
    def ctr_crypt(cls, key: bytes, initial_counter: bytes, data: bytes) -> bytes:
        rks = cls.expand_key(key)
        out = bytearray()
        ctr = int.from_bytes(initial_counter, "big")
        for off in range(0, len(data), 16):
            stream = cls.encrypt_block(ctr.to_bytes(16, "big"), rks)
            out += bytes(a ^ b for a, b in zip(data[off:off + 16], stream))
            ctr = (ctr + 1) & ((1 << 128) - 1)
        return bytes(out)


def _chacha20_block(key: bytes, counter16: bytes) -> bytes:
    state = struct.unpack("<4I", b"expand 32-byte k") + struct.unpack("<8I", key) + struct.unpack("<4I", counter16)

    def qr(s, a, b, c, d):
        s[a] = (s[a] + s[b]) & 0xFFFFFFFF; s[d] ^= s[a]; s[d] = ((s[d] << 16) | (s[d] >> 16)) & 0xFFFFFFFF
        s[c] = (s[c] + s[d]) & 0xFFFFFFFF; s[b] ^= s[c]; s[b] = ((s[b] << 12) | (s[b] >> 20)) & 0xFFFFFFFF
        s[a] = (s[a] + s[b]) & 0xFFFFFFFF; s[d] ^= s[a]; s[d] = ((s[d] << 8) | (s[d] >> 24)) & 0xFFFFFFFF
        s[c] = (s[c] + s[d]) & 0xFFFFFFFF; s[b] ^= s[c]; s[b] = ((s[b] << 7) | (s[b] >> 25)) & 0xFFFFFFFF

    mixed = list(state)
    for _ in range(10):
        qr(mixed, 0, 4, 8, 12); qr(mixed, 1, 5, 9, 13); qr(mixed, 2, 6, 10, 14); qr(mixed, 3, 7, 11, 15)
        qr(mixed, 0, 5, 10, 15); qr(mixed, 1, 6, 11, 12); qr(mixed, 2, 7, 8, 13); qr(mixed, 3, 4, 9, 14)
    return struct.pack("<16I", *[(a + b) & 0xFFFFFFFF for a, b in zip(mixed, state)])


def _stdlib_rc4(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(byte ^ S[(S[i] + S[j]) & 0xFF])
    return bytes(out)


def _stdlib_backend():
    class Backend:
        @staticmethod
        def aes_cbc(key, iv, data):
            return _StdlibAES.cbc_decrypt(key, iv, data)

        @staticmethod
        def aes_gcm(key, nonce, data):
            # GCM keystream starts at counter block 2 (counter 1 is reserved for the tag path)
            return _StdlibAES.ctr_crypt(key, nonce + b"\x00\x00\x00\x02", data), bytes(16)

        @staticmethod
        def chacha20(key, nonce, data):
            while len(nonce) < 12:
                nonce = b"\x00" + nonce
            out = bytearray()
            base = int.from_bytes(nonce[8:12], "little")
            for index, off in enumerate(range(0, len(data), 64)):
                counter = (base + index) & 0xFFFFFFFF
                keystream = _chacha20_block(key, struct.pack("<I", counter) + nonce)
                out += bytes(a ^ b for a, b in zip(data[off:off + 64], keystream))
            return bytes(out)

        @staticmethod
        def rc4(key, data):
            return _stdlib_rc4(key, data)

    return Backend()


def _crypto():
    """A backend namespace for aes_cbc/aes_gcm/chacha20/rc4, or the stdlib fallback."""
    try:
        from Crypto.Cipher import AES, ARC4, ChaCha20  # pycryptodome

        class _PyCryptodome:
            @staticmethod
            def aes_cbc(key, iv, data):
                return AES.new(key, AES.MODE_CBC, iv).decrypt(data)

            @staticmethod
            def aes_gcm(key, nonce, data):
                return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt(data), bytes(16)

            @staticmethod
            def chacha20(key, nonce, data):
                return ChaCha20.new(key=key, nonce=nonce).decrypt(data)

            @staticmethod
            def rc4(key, data):
                return ARC4.new(key).decrypt(data)

        return _PyCryptodome()
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        class _Cryptography:
            @staticmethod
            def aes_cbc(key, iv, data):
                dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
                return dec.update(data) + dec.finalize()

            @staticmethod
            def aes_gcm(key, nonce, data):
                dec = Cipher(algorithms.AES(key), modes.GCM(nonce)).decryptor()
                return dec.update(data), bytes(16)

            @staticmethod
            def chacha20(key, nonce, data):
                dec = Cipher(algorithms.ChaCha20(key), nonce).decryptor()
                return dec.update(data)

            @staticmethod
            def rc4(key, data):
                return _stdlib_rc4(key, data)

        return _Cryptography()
    except ImportError:
        return _stdlib_backend()


# --------------------------------------------------------------------------- data model

@dataclass(frozen=True)
class Candidate:
    """One SHA-256-verified array constant recovered from the metadata."""
    typedef: int
    field_index: int  # index within the declaring class
    name: str  # the SHA-256 hex field name
    size: int
    data: bytes


@dataclass(frozen=True)
class Solution:
    scheme: str  # one of SCHEMES
    key: bytes
    nonce: bytes | None  # iv (cbc/gcm) or nonce (gcm/chacha20); None for rc4/xor
    magic: bytes  # the prefix stripped before decryption
    origin: str  # human-readable provenance of the key


@dataclass
class DecryptResult:
    root: str
    files: list[dict] = field(default_factory=list)  # {name, bytes_in, bytes_out, status}
    solution: Solution | None = None
    skipped: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- crypto dispatch

def _decrypt_body(backend, scheme: str, key: bytes, nonce: bytes | None, body: bytes) -> bytes:
    """Decrypt the ciphertext body; `body` is already stripped of the magic prefix."""
    if scheme == "aes-128-cbc":
        pad = (-len(body)) % 16
        return backend.aes_cbc(key, nonce, body + b"\x00" * pad)[:len(body)]
    if scheme == "aes-128-gcm":
        return backend.aes_gcm(key, nonce, body)[0]
    if scheme == "chacha20":
        return backend.chacha20(key, nonce, body)
    if scheme == "rc4":
        return backend.rc4(key, body)
    if scheme == "xor":
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(body))
    raise HotDllError(f"unknown scheme {scheme!r}")


def _pe_head_ok(data: bytes) -> bool:
    """Cheap first-bytes filter for the probe stage: MZ and a sane e_lfanew."""
    if len(data) < 2 or data[:2] != b"MZ":
        return False
    if len(data) >= 0x40:
        lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if not 0x40 <= lfanew < 0x400:
            return False
    return True


def _looks_like_dotnet_pe(data: bytes) -> bool:
    """A decrypted HybridCLR hot-update assembly is a PE32(+) with a sane header."""
    if len(data) < 0x100 or data[:2] != b"MZ":
        return False
    lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if not 0x40 <= lfanew < len(data) - 0x40 or data[lfanew:lfanew + 4] != b"PE\x00\x00":
        return False
    machine = struct.unpack_from("<H", data, lfanew + 4)[0]
    return machine in (0x14C, 0x8664)  # I386 / AMD64


# --------------------------------------------------------------------------- key recovery

def extract_key_candidates(metadata_path: Path) -> list[Candidate]:
    """Every SHA-256-verified array constant of every <PrivateImplementationDetails> class.

    The C# compiler names these fields after the SHA-256 of their own bytes, so every
    candidate below is verified against its own name; nothing is guessed."""
    from uare.il2cpp import METADATA_MAGIC

    data = metadata_path.read_bytes()
    if len(data) < 8 + 31 * 8:
        raise Il2CppMetadataError(f"{metadata_path} is too small to be IL2CPP metadata")
    magic, version = struct.unpack_from("<Ii", data, 0)
    if magic != METADATA_MAGIC:
        raise Il2CppMetadataError(f"{metadata_path}: bad magic (encrypted metadata?); "
                                  "run recover-metadata first")
    if version not in _SUPPORTED_VERSIONS:
        raise HotDllError(f"metadata v{version} is not supported (need v27-v31 for the "
                          "fieldDefaultValues layout)")
    pairs = [struct.unpack_from("<II", data, 8 + i * 8) for i in range(31)]
    heap_off, heap_size = pairs[_SEC_STRING]
    fdv_off, fdv_size = pairs[_SEC_FDV]
    blob_off, blob_size = pairs[_SEC_BLOB]
    fields_off, fields_size = pairs[_SEC_FIELDS]
    types_off, types_size = pairs[_SEC_TYPES]
    for name, off, size in (("fieldDefaultValues", fdv_off, fdv_size), ("blob", blob_off, blob_size),
                            ("fields", fields_off, fields_size), ("types", types_off, types_size)):
        if off + size > len(data):
            raise Il2CppMetadataError(f"section {name} [{off}, +{size}) exceeds the file size")
    heap = data[heap_off:heap_off + heap_size]
    type_count = types_size // _TYPE_STRIDE
    if not 0 < type_count < _MAX_TYPE_COUNT:
        raise Il2CppMetadataError(f"implausible type table size {types_size}")

    def name_at(index: int) -> str:
        if not 0 <= index < len(heap):
            return ""
        end = heap.find(b"\x00", index)
        return heap[index:end].decode("utf-8", "replace")

    fdv: dict[int, int] = {}  # global field index -> blob data index
    for i in range(fdv_size // _FDV_STRIDE):
        fi, _type_index, di = struct.unpack_from("<iii", data, fdv_off + i * _FDV_STRIDE)
        fdv[fi] = di

    out: list[Candidate] = []
    for td in range(type_count):
        base = types_off + td * _TYPE_STRIDE
        if name_at(struct.unpack_from("<i", data, base)[0]) != "<PrivateImplementationDetails>":
            continue
        fstart = struct.unpack_from("<i", data, base + 32)[0]
        fcount = struct.unpack_from("<H", data, base + 68)[0]
        for j in range(fcount):
            di = fdv.get(fstart + j)
            if di is None or not 0 <= di < blob_size:
                continue
            fname = name_at(struct.unpack_from("<i", data, fields_off + (fstart + j) * _FIELD_STRIDE)[0])
            if len(fname) != 64:
                continue
            want = fname.upper()
            for size in range(1, _MAX_KEY_SIZE + 1):
                chunk = data[blob_off + di:blob_off + di + size]
                if hashlib.sha256(chunk).hexdigest().upper() != want:
                    continue
                out.append(Candidate(td, j, fname, size, bytes(chunk)))
                break
    event(LOG, logging.INFO, "hotdll_candidates", metadata=str(metadata_path), count=len(out))
    return out


def _common_prefix(blobs: list[bytes]) -> bytes:
    if not blobs:
        return b""
    prefix = blobs[0]
    for blob in blobs[1:]:
        while prefix and not blob.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def find_solution(blobs: list[bytes], candidates: list[Candidate], backend=None) -> Solution:
    """Try every scheme/key combination against the smallest blob until one yields a valid PE.

    A candidate is accepted only when the whole sample decrypts to a PE (and, for AES-CBC,
    ends with consistent PKCS#7 padding); anything else raises instead of guessing."""
    if not blobs:
        raise HotDllError("no encrypted assemblies given")
    if not candidates:
        raise HotDllError("no key candidates recovered from the metadata")
    backend = backend or _crypto()
    sample = min(blobs, key=len)
    prefix = _common_prefix(blobs)
    magic_lengths: list[int] = []
    for length in (*_MAGIC_LENGTHS, len(prefix)):
        if 0 <= length < len(sample) and length not in magic_lengths:
            magic_lengths.append(length)

    keys_16 = [c for c in candidates if c.size == 16]
    keys_12 = [c for c in candidates if c.size == 12]
    keys_32 = [c for c in candidates if c.size == 32]

    def origin(c: Candidate) -> str:
        return (f"metadata <PrivateImplementationDetails> typedef {c.typedef} field {c.field_index} "
                f"(sha256 {c.name[:12]}..., {c.size} bytes)")

    def attempt(solution: Solution) -> Solution | None:
        head = _decrypt_body(backend, solution.scheme, solution.key, solution.nonce,
                            sample[len(solution.magic):len(solution.magic) + 0x40])
        if not _pe_head_ok(head):
            return None
        plain = _decrypt_body(backend, solution.scheme, solution.key, solution.nonce,
                               sample[len(solution.magic):])
        if not _looks_like_dotnet_pe(plain):
            return None
        if solution.scheme == "aes-128-cbc":
            if len(plain) % 16:
                return None
            pad = plain[-1]
            if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
                return None
        return solution

    for magic_len in sorted(magic_lengths):
        magic = sample[:magic_len]
        for kc in keys_16:
            for ivc in keys_16:  # AES-128-CBC: key x IV
                solution = attempt(Solution("aes-128-cbc", kc.data, ivc.data, magic,
                                            f"key: {origin(kc)}; iv: {origin(ivc)}"))
                if solution:
                    return solution
            for nc in keys_12:  # AES-128-GCM: key x 12-byte nonce
                solution = attempt(Solution("aes-128-gcm", kc.data, nc.data, magic,
                                            f"key: {origin(kc)}; nonce: {origin(nc)}"))
                if solution:
                    return solution
            for scheme in ("rc4", "xor"):  # stream schemes with a 16-byte key
                solution = attempt(Solution(scheme, kc.data, None, magic, f"key: {origin(kc)}"))
                if solution:
                    return solution
        for kc in keys_32:
            for nc in keys_12:  # ChaCha20: 32-byte key x 12-byte nonce
                solution = attempt(Solution("chacha20", kc.data, nc.data, magic,
                                            f"key: {origin(kc)}; nonce: {origin(nc)}"))
                if solution:
                    return solution
    raise HotDllError(
        f"no scheme/key combination decrypted the sample: {len(candidates)} candidates were "
        "recovered and SHA-256 verified, but none of AES-128-CBC/GCM, ChaCha20, RC4 or XOR "
        "yielded a PE. The game may use a custom scheme or keys outside the metadata; supply "
        "--algo/--key/--iv/--magic to decrypt directly.")


def _solution_from_args(algo: str | None, key: bytes | None, iv: bytes | None,
                        magic: bytes | None) -> Solution:
    if algo not in SCHEMES:
        raise HotDllError(f"--algo must be one of: {', '.join(SCHEMES)}")
    if not key:
        raise HotDllError("--key is required when keys are passed explicitly")
    if algo in ("aes-128-cbc", "aes-128-gcm", "chacha20") and not iv:
        raise HotDllError(f"--iv/--nonce is required for {algo}")
    return Solution(algo, key, iv, magic or b"", "user-supplied key")


def _locate_metadata(dll_dir: Path) -> Path:
    """Walk up from the dll directory looking for an extracted tree's metadata."""
    for parent in (dll_dir, *dll_dir.parents):
        for candidate in (parent / "Data" / "Managed" / "Metadata" / "global-metadata.dat",
                          parent / "extracted" / "Data" / "Managed" / "Metadata" / "global-metadata.dat"):
            if candidate.is_file():
                return candidate
    raise HotDllError(
        "global-metadata.dat not found near the dll directory; pass --metadata explicitly "
        "(it lives under <extracted>/Data/Managed/Metadata/)")


# --------------------------------------------------------------------------- command entry

def decrypt_directory(dll_dir: Path, out_dir: Path, *, metadata: Path | None = None,
                      algo: str | None = None, key: bytes | None = None,
                      iv: bytes | None = None, magic: bytes | None = None,
                      keys_file: Path | None = None) -> DecryptResult:
    """Decrypt every encrypted hot-update assembly in `dll_dir` into `out_dir` (must not exist)."""
    if not dll_dir.is_dir():
        raise HotDllError(f"{dll_dir} is not a directory")
    files = sorted(p for p in dll_dir.iterdir() if p.is_file() and p.suffix.lower() == ".dll")
    if not files:
        raise HotDllError(f"no .dll files under {dll_dir}")
    blobs = {p.name: p.read_bytes() for p in files}
    encrypted = {name: blob for name, blob in blobs.items() if not _looks_like_dotnet_pe(blob)}
    if not encrypted:
        raise HotDllError("every .dll here is already a plain PE; nothing to decrypt")

    backend = _crypto()
    if keys_file is not None:
        spec = json.loads(keys_file.read_text(encoding="utf-8"))
        solution = Solution(spec["algo"], bytes.fromhex(spec["key"]),
                            bytes.fromhex(spec["nonce"]) if spec.get("nonce") else None,
                            bytes.fromhex(spec.get("magic", "")) or b"",
                            spec.get("origin", "keys file"))
    elif key is not None:
        solution = _solution_from_args(algo, key, iv, magic)
    else:
        candidates = extract_key_candidates(metadata or _locate_metadata(dll_dir))
        solution = find_solution([encrypted[name] for name in sorted(encrypted)], candidates, backend)

    result = DecryptResult(root=str(dll_dir), solution=solution)
    with staged_output(out_dir) as staging:
        (staging / "decrypted").mkdir()
        for name in sorted(blobs):
            blob = blobs[name]
            if name not in encrypted:
                result.skipped.append(name)
                continue
            if solution.magic and blob[:len(solution.magic)] != solution.magic:
                result.files.append({"name": name, "bytes_in": len(blob), "bytes_out": 0,
                                     "status": "magic mismatch; copied as-is"})
                (staging / "decrypted" / name).write_bytes(blob)
                continue
            plain = _decrypt_body(backend, solution.scheme, solution.key, solution.nonce,
                                  blob[len(solution.magic):])
            if solution.scheme == "aes-128-cbc" and plain and 1 <= plain[-1] <= 16 \
                    and plain[-plain[-1]:] == bytes([plain[-1]]) * plain[-1]:
                plain = plain[:-plain[-1]]
            if not _looks_like_dotnet_pe(plain):
                result.files.append({"name": name, "bytes_in": len(blob), "bytes_out": 0,
                                     "status": "decryption did not yield a PE; copied as-is"})
                (staging / "decrypted" / name).write_bytes(blob)
                continue
            (staging / "decrypted" / name).write_bytes(plain)
            result.files.append({"name": name, "bytes_in": len(blob), "bytes_out": len(plain),
                                 "status": "decrypted"})
        (staging / "keys.json").write_text(json.dumps({
            "algo": solution.scheme,
            "key": solution.key.hex(),
            "nonce": solution.nonce.hex() if solution.nonce else None,
            "magic": solution.magic.hex(),
            "origin": solution.origin,
        }, indent=2), encoding="utf-8")
        (staging / "report.json").write_text(json.dumps({
            "root": result.root,
            "solution": {"algo": solution.scheme, "key": solution.key.hex(),
                         "nonce": solution.nonce.hex() if solution.nonce else None,
                         "magic": solution.magic.hex(), "origin": solution.origin},
            "files": result.files, "skipped": result.skipped,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    event(LOG, logging.INFO, "hotdll_done", root=str(dll_dir),
          decrypted=sum(1 for f in result.files if f["status"] == "decrypted"))
    return result


def summarize(result: DecryptResult) -> list[str]:
    lines = [f"directory        : {result.root}"]
    if result.solution:
        lines.append(f"scheme           : {result.solution.scheme}")
        lines.append(f"key              : {result.solution.key.hex()}")
        if result.solution.nonce:
            lines.append(f"iv/nonce         : {result.solution.nonce.hex()}")
        lines.append(f"magic prefix     : {result.solution.magic.hex() or '-'}")
        lines.append(f"key origin       : {result.solution.origin}")
    decrypted = sum(1 for f in result.files if f["status"] == "decrypted")
    lines.append(f"decrypted        : {decrypted}/{len(result.files)} -> decrypted/")
    if result.skipped:
        lines.append(f"already plain    : {len(result.skipped)} (copied as-is)")
    failed = [f["name"] for f in result.files if f["status"] != "decrypted"]
    if failed:
        lines.append(f"failed           : {', '.join(failed)}")
    lines.append("keys             : keys.json (pass back via --keys to skip the search)")
    return lines
