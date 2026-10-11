"""Extract Wwise sound banks (.bnk) to individual WEM audio files.

Wwise sound banks use a section-based binary format:

- **BKHD** (Bank Header): version, bank ID, language
- **DIDX** (Data Index): array of (WEM ID, offset into DATA, size) entries
- **DATA**: raw concatenation of embedded WEM files

Each WEM is a complete audio container (Vorbis or Opus inside a RIFF wrapper).
This module reads the BKHD/DIDX/DATA sections, extracts each embedded WEM to
its own file, and writes a manifest listing all extracted entries.
"""

import json
import logging
import struct
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.wwise")

_SECTION_HEADER = struct.Struct("<4sI")
_DIDX_ENTRY = struct.Struct("<III")


class WwiseFormatError(UareError):
    """A .bnk file is not a valid Wwise sound bank."""


@dataclass(frozen=True)
class WemEntry:
    wem_id: int
    offset: int
    size: int
    output: str


@dataclass(frozen=True)
class BankResult:
    bank_path: str
    bank_id: int
    version: int
    wem_count: int
    total_bytes: int
    entries: tuple[WemEntry, ...]
    error: str | None


@dataclass(frozen=True)
class WwiseResult:
    banks_found: int
    banks_extracted: int
    total_wems: int
    total_bytes: int
    failed: int
    banks: tuple[BankResult, ...]
    out_dir: str


def _read_sections(data: bytes) -> dict[str, tuple[int, int]]:
    """Parse a .bnk into {section_tag: (data_offset, data_length)}."""
    sections: dict[str, tuple[int, int]] = {}
    pos = 0
    while pos + _SECTION_HEADER.size <= len(data):
        tag_raw, length = _SECTION_HEADER.unpack_from(data, pos)
        try:
            tag = tag_raw.decode("ascii")
        except UnicodeDecodeError:
            break
        data_start = pos + _SECTION_HEADER.size
        if data_start + length > len(data):
            break
        sections[tag] = (data_start, length)
        pos = data_start + length
    return sections


def _parse_bkhd(data: bytes, offset: int, length: int) -> tuple[int, int]:
    """Return (version, bank_id) from BKHD section."""
    if length < 8:
        raise WwiseFormatError("BKHD section too short")
    version, bank_id = struct.unpack_from("<II", data, offset)
    return version, bank_id


def _parse_didx(data: bytes, offset: int, length: int) -> list[tuple[int, int, int]]:
    """Return list of (wem_id, data_offset, size) from DIDX section."""
    if length % _DIDX_ENTRY.size != 0:
        raise WwiseFormatError(f"DIDX section length {length} not divisible by entry size {_DIDX_ENTRY.size}")
    entries: list[tuple[int, int, int]] = []
    pos = offset
    end = offset + length
    while pos < end:
        wem_id, wem_offset, wem_size = _DIDX_ENTRY.unpack_from(data, pos)
        entries.append((wem_id, wem_offset, wem_size))
        pos += _DIDX_ENTRY.size
    return entries


def extract_bank(bnk_path: Path, out_dir: Path) -> BankResult:
    """Extract all WEM files from a single .bnk sound bank."""
    raw = bnk_path.read_bytes()
    sections = _read_sections(raw)

    if "BKHD" not in sections:
        raise WwiseFormatError(f"{bnk_path.name}: missing BKHD section")

    bkhd_off, bkhd_len = sections["BKHD"]
    version, bank_id = _parse_bkhd(raw, bkhd_off, bkhd_len)

    if "DIDX" not in sections or "DATA" not in sections:
        return BankResult(str(bnk_path), bank_id, version, 0, 0, (), None)

    didx_off, didx_len = sections["DIDX"]
    data_off, data_len = sections["DATA"]
    index = _parse_didx(raw, didx_off, didx_len)

    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list[WemEntry] = []
    total_bytes = 0

    for wem_id, wem_offset, wem_size in index:
        abs_offset = data_off + wem_offset
        if abs_offset + wem_size > len(raw):
            event(LOG, logging.WARNING, "wem_truncated", bank=bnk_path.name,
                  wem_id=wem_id, expected=wem_size, available=len(raw) - abs_offset)
            continue
        wem_data = raw[abs_offset:abs_offset + wem_size]
        filename = f"{wem_id}.wem"
        (out_dir / filename).write_bytes(wem_data)
        entries.append(WemEntry(wem_id, wem_offset, wem_size, filename))
        total_bytes += wem_size

    return BankResult(str(bnk_path), bank_id, version, len(entries), total_bytes,
                      tuple(entries), None)


def extract_all(source_dir: Path, out_dir: Path) -> WwiseResult:
    """Find and extract all .bnk Wwise sound banks under source_dir."""
    if not source_dir.is_dir():
        raise UareError(f"{source_dir} is not a directory")

    bnk_files: list[Path] = sorted(source_dir.rglob("*.bnk"))
    banks: list[BankResult] = []
    extracted = failed = total_wems = total_bytes = 0

    with staged_output(out_dir) as staging:
        for bnk in bnk_files:
            bank_name = bnk.stem
            bank_out = staging / bank_name
            try:
                result = extract_bank(bnk, bank_out)
                banks.append(result)
                extracted += 1
                total_wems += result.wem_count
                total_bytes += result.total_bytes
                event(LOG, logging.INFO, "bank_extracted", bank=bank_name,
                      wems=result.wem_count, total_bytes=result.total_bytes)
            except (WwiseFormatError, OSError) as exc:
                failed += 1
                banks.append(BankResult(str(bnk), 0, 0, 0, 0, (), str(exc)))
                event(LOG, logging.WARNING, "bank_failed", name=bank_name, error=str(exc))

        (staging / "wwise_report.json").write_text(
            json.dumps({"banks_found": len(bnk_files), "banks_extracted": extracted,
                        "total_wems": total_wems, "total_bytes": total_bytes, "failed": failed,
                        "banks": [{"path": b.bank_path, "bank_id": b.bank_id,
                                   "version": b.version, "wems": b.wem_count} for b in banks]},
                       indent=2, ensure_ascii=False), encoding="utf-8")

    event(LOG, logging.INFO, "wwise_done", banks=len(bnk_files), extracted=extracted,
          wems=total_wems, failed=failed)
    return WwiseResult(len(bnk_files), extracted, total_wems, total_bytes, failed,
                       tuple(banks), str(out_dir))


def summarize(result: WwiseResult) -> list[str]:
    return [
        f"banks found       : {result.banks_found}",
        f"banks extracted   : {result.banks_extracted}",
        f"total WEM files   : {result.total_wems}",
        f"total audio bytes : {result.total_bytes}",
        f"failed            : {result.failed}",
        f"output            : {result.out_dir}",
    ]
