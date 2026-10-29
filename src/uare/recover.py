"""Recover global-metadata.dat from common obfuscation wrappers (opt-in deobfuscation).

This is the toolkit's one deliberate boundary crossing. The default pipeline still refuses to
touch metadata whose magic is wrong and reports it as encrypted. This module is the explicit,
opt-in step that tries to undo a reversible wrapper first.

What it does:

- identity       : the magic is already correct; nothing to do.
- xor-key        : a caller-supplied repeating XOR key (hex), tried as-is.
- xor-single-byte: derive the one key byte from the first magic byte and verify the whole header.
- xor-repeating  : derive a key of length 1-4 from the four known magic bytes and verify.
- external       : run a caller-supplied command (`{input}`/`{output}` placeholders, or stdout)
                   to undo a scheme this module does not implement.

It never guesses: a candidate is accepted only if the recovered bytes carry the real magic
(0xFAB11BAF) and a plausible metadata version. Anything else raises Il2CppMetadataError.
"""

import logging
import struct
from dataclasses import dataclass
from pathlib import Path

from uare.errors import Il2CppMetadataError
from uare.external import DEFAULT_TIMEOUT_S, run_command
from uare.il2cpp import METADATA_MAGIC
from uare.logs import event

LOG = logging.getLogger("uare.recover")

_MAGIC_LE = struct.pack("<I", METADATA_MAGIC)
_VERSION_MIN, _VERSION_MAX = 16, 40


@dataclass(frozen=True)
class Recovery:
    data: bytes
    scheme: str  # identity | xor-key | xor-single-byte | xor-repeating | external


def header_version(data: bytes) -> int | None:
    """Metadata version if the bytes carry a valid header, else None."""
    if len(data) < 8:
        return None
    magic, version = struct.unpack_from("<Ii", data, 0)
    if magic == METADATA_MAGIC and _VERSION_MIN <= version <= _VERSION_MAX:
        return version
    return None


def _xor(data: bytes, key: bytes) -> bytes:
    if len(key) == 1:
        table = bytes(b ^ key[0] for b in range(256))
        return data.translate(table)
    repeated = key * (len(data) // len(key) + 1)
    return bytes(a ^ b for a, b in zip(data, repeated))


def _candidate_keys(data: bytes) -> list[tuple[str, bytes]]:
    """XOR keys derivable from the known first four plaintext bytes (the little-endian magic)."""
    keys: list[tuple[str, bytes]] = []
    if not data:
        return keys
    for length in range(1, 5):
        if len(data) < length:
            continue
        key = bytes(data[i] ^ _MAGIC_LE[i % 4] for i in range(length))
        keys.append((f"xor-repeating(len={length})", key))
    return keys


def recover(data: bytes, *, key: bytes | None = None, command: str | None = None,
            command_timeout_s: float = DEFAULT_TIMEOUT_S) -> Recovery:
    """Return the recovered metadata bytes and the scheme that worked, or raise."""
    if header_version(data) is not None:
        return Recovery(data, "identity")

    if key:
        candidate = _xor(data, key)
        if header_version(candidate) is not None:
            event(LOG, logging.INFO, "metadata_recovered", scheme="xor-key", key_len=len(key))
            return Recovery(candidate, "xor-key")

    if command:
        candidate = run_command(command, data, command_timeout_s)
        if header_version(candidate) is not None:
            event(LOG, logging.INFO, "metadata_recovered", scheme="external")
            return Recovery(candidate, "external")
        raise Il2CppMetadataError(
            "external recovery command ran but the result still has no valid metadata header")

    for scheme, guess in _candidate_keys(data):
        candidate = _xor(data, guess)
        if header_version(candidate) is not None:
            label = "xor-single-byte" if len(guess) == 1 else "xor-repeating"
            event(LOG, logging.INFO, "metadata_recovered", scheme=label, key_len=len(guess))
            return Recovery(candidate, label)

    raise Il2CppMetadataError(
        "could not recover metadata: not a single/short repeating XOR of the header. "
        "Supply --key (hex) or --cmd with a game-specific decryptor.")


def recover_file(path: Path, *, key: bytes | None = None, command: str | None = None,
                 command_timeout_s: float = DEFAULT_TIMEOUT_S) -> Recovery:
    if not path.is_file():
        raise Il2CppMetadataError(f"{path} does not exist")
    return recover(path.read_bytes(), key=key, command=command, command_timeout_s=command_timeout_s)
