"""ELF inspection for Android native libraries: segments, dynamic symbols, entropy.

The packing verdict is a report only. A high-entropy executable segment means static
analysis of method bodies will not work on this file; this module does not unpack it.
"""

import math
import mmap
import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from uare.errors import ElfFormatError
from uare.packers import identify as identify_packer

PT_LOAD = 1
PF_X, PF_W, PF_R = 1, 2, 4
SHT_DYNSYM = 11
SHN_UNDEF = 0
WINDOW_SIZE = 1 << 20
MIN_WINDOW_FOR_VERDICT = 64 << 10  # shorter tail windows cannot reach high entropy and are not counted
MAX_SYMBOL_NAME = 4096
PACKED_ENTROPY = 7.2
NORMAL_ENTROPY = 7.0  # unpacked aarch64 code measures 5.7-6.95 (libmain, libunity 6000.2)
HIGH_WINDOW_ENTROPY = 7.5
FILL_WINDOW_ENTROPY = 1.0
HIGH_WINDOW_FRACTION = 0.25  # share of high-entropy windows that already means "protected"
FILL_FRACTION_PACKED = 0.5  # encrypted chunk plus this much filler is a packer signature
FILL_FRACTION_HINT = 0.25  # below the packed bar, still corroborates a protected layout
_MACHINES = {3: "x86", 40: "arm", 62: "x86_64", 183: "aarch64"}
_ENTRY_SIZES = {2: (56, 64, 24), 1: (32, 40, 16)}  # class -> (phentsize, shentsize, symentsize)


@dataclass(frozen=True)
class Segment:
    index: int
    flags: str
    offset: int
    file_size: int
    vaddr: int
    mem_size: int
    entropy: float | None


@dataclass(frozen=True)
class SymbolSummary:
    section_table: str  # ok | absent | out_of_bounds
    defined: int | None
    imported: int | None
    il2cpp_exports: tuple[str, ...]


@dataclass(frozen=True)
class ElfReport:
    path: str
    size: int
    elf_class: int
    machine: str
    load_segments: tuple[Segment, ...]
    section_count: int
    symbols: SymbolSummary
    exec_entropy: float | None
    exec_windows: int
    high_entropy_windows: int
    fill_windows: int
    packing_verdict: str  # normal | inconclusive | likely_packed_or_encrypted | no_exec_segment
    packer: str | None  # name of a recognised packer, or None


@dataclass(frozen=True)
class _Layout:
    endian: str
    elf_class: int
    machine: int
    phoff: int
    phnum: int
    shoff: int
    shnum: int


@dataclass(frozen=True)
class _ProgramHeader:
    type: int
    flags: int
    offset: int
    vaddr: int
    file_size: int
    mem_size: int


@dataclass(frozen=True)
class _WindowStats:
    counts: Counter[int]
    windows: int
    high: int
    fill: int


def shannon_entropy(counts: Counter[int], total: int) -> float:
    if total == 0:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in counts.values() if c)


def _unpack(fmt: str, buf: "mmap.mmap | bytes", offset: int) -> tuple[int, ...]:
    if offset < 0 or offset + struct.calcsize(fmt) > len(buf):
        raise ElfFormatError(f"structure {fmt} at offset {offset} lies outside the file")
    return struct.unpack_from(fmt, buf, offset)


def _parse_layout(buf: "mmap.mmap | bytes") -> _Layout:
    if len(buf) < 16 or bytes(buf[:4]) != b"\x7fELF":
        raise ElfFormatError("missing ELF magic")
    elf_class, data_enc = buf[4], buf[5]
    if elf_class not in (1, 2) or data_enc not in (1, 2):
        raise ElfFormatError(f"unsupported ELF class {elf_class} / encoding {data_enc}")
    e = "<" if data_enc == 1 else ">"
    fmt = f"{e}HHIQQQIHHHHHH" if elf_class == 2 else f"{e}HHIIIIIHHHHHH"
    (_t, machine, _v, _entry, phoff, shoff, _f, _eh, phentsize, phnum, shentsize, shnum, _shstr) = _unpack(fmt, buf, 16)
    want_ph, want_sh, _sym = _ENTRY_SIZES[elf_class]
    if phnum and phentsize != want_ph:
        raise ElfFormatError(f"e_phentsize {phentsize} != {want_ph}")
    if shnum and shentsize != want_sh:
        raise ElfFormatError(f"e_shentsize {shentsize} != {want_sh}")
    return _Layout(e, elf_class, machine, phoff, phnum, shoff, shnum)


def _flags(value: int) -> str:
    return ("R" if value & PF_R else "-") + ("W" if value & PF_W else "-") + ("X" if value & PF_X else "-")


def _program_headers(buf: "mmap.mmap | bytes", lay: _Layout) -> list[_ProgramHeader]:
    rows: list[_ProgramHeader] = []
    size = _ENTRY_SIZES[lay.elf_class][0]
    for i in range(lay.phnum):
        at = lay.phoff + i * size
        if lay.elf_class == 2:
            p_type, p_flags, p_offset, p_vaddr, _pa, p_filesz, p_memsz, _al = _unpack(f"{lay.endian}IIQQQQQQ", buf, at)
        else:
            p_type, p_offset, p_vaddr, _pa, p_filesz, p_memsz, p_flags, _al = _unpack(f"{lay.endian}IIIIIIII", buf, at)
        if p_offset + p_filesz > len(buf):
            raise ElfFormatError(f"program header {i} file range exceeds file size")
        rows.append(_ProgramHeader(p_type, p_flags, p_offset, p_vaddr, p_filesz, p_memsz))
    return rows


def _symbols(buf: "mmap.mmap | bytes", lay: _Layout) -> SymbolSummary:
    if lay.shoff == 0 or lay.shnum == 0:
        return SymbolSummary("absent", None, None, ())
    _ph, sh_size, sym_entsize = _ENTRY_SIZES[lay.elf_class]
    if lay.shoff + lay.shnum * sh_size > len(buf):
        return SymbolSummary("out_of_bounds", None, None, ())
    sections: list[tuple[int, int, int, int]] = []  # (type, offset, size, link)
    for i in range(lay.shnum):
        at = lay.shoff + i * sh_size
        if lay.elf_class == 2:
            _n, sh_type, _fl, _ad, sh_offset, size, link, _in, _al, _es = _unpack(f"{lay.endian}IIQQQQIIQQ", buf, at)
        else:
            _n, sh_type, _fl, _ad, sh_offset, size, link, _in, _al, _es = _unpack(f"{lay.endian}IIIIIIIIII", buf, at)
        sections.append((sh_type, sh_offset, size, link))
    dynsyms = [s for s in sections if s[0] == SHT_DYNSYM]
    if not dynsyms:
        return SymbolSummary("ok", 0, 0, ())
    _t, sym_off, sym_size, link = dynsyms[0]
    if link >= len(sections):
        raise ElfFormatError(f"SHT_DYNSYM links to missing section {link}")
    _st, str_off, str_size, _l = sections[link]
    if sym_off + sym_size > len(buf) or str_off + str_size > len(buf):
        raise ElfFormatError("dynamic symbol or string table lies outside the file")
    strtab = bytes(buf[str_off:str_off + str_size])
    # Elf64_Sym: name@0, shndx@6; Elf32_Sym: name@0, shndx@14.
    shndx_at = 6 if lay.elf_class == 2 else 14
    defined = imported = 0
    exports: list[str] = []
    for i in range(1, sym_size // sym_entsize):
        at = sym_off + i * sym_entsize
        (st_name,) = _unpack(f"{lay.endian}I", buf, at)
        (st_shndx,) = _unpack(f"{lay.endian}H", buf, at + shndx_at)
        if st_shndx == SHN_UNDEF:
            imported += 1
            continue
        defined += 1
        if st_name == 0:
            continue
        if st_name >= len(strtab):
            raise ElfFormatError(f"symbol {i} name offset {st_name} outside .dynstr")
        end = strtab.find(b"\x00", st_name, st_name + MAX_SYMBOL_NAME)
        if end < 0:
            raise ElfFormatError(f"symbol {i} name is unterminated or longer than {MAX_SYMBOL_NAME} bytes")
        if strtab.startswith(b"il2cpp_", st_name):
            exports.append(strtab[st_name:end].decode("ascii", errors="backslashreplace"))
    return SymbolSummary("ok", defined, imported, tuple(sorted(exports)))


def _window_stats(buf: "mmap.mmap | bytes", offset: int, size: int) -> _WindowStats:
    total: Counter[int] = Counter()
    windows = high = fill = 0
    for start in range(offset, offset + size, WINDOW_SIZE):
        chunk = bytes(buf[start:min(start + WINDOW_SIZE, offset + size)])
        counts = Counter(chunk)
        total.update(counts)
        if len(chunk) < MIN_WINDOW_FOR_VERDICT and windows:
            continue
        entropy = shannon_entropy(counts, len(chunk))
        windows += 1
        high += entropy >= HIGH_WINDOW_ENTROPY
        fill += entropy <= FILL_WINDOW_ENTROPY
    return _WindowStats(total, windows, high, fill)


def verdict(exec_entropy: float | None, windows: int, high_windows: int, fill_windows: int = 0,
            section_table: str = "ok", defined_symbols: int = 0) -> str:
    """Packing verdict from entropy plus structural corroboration.

    Averages alone are weak (a filler region dilutes them), so the window mix is the primary
    signal. Only in the ambiguous 7.0-7.2 band does structure decide: a broken section table or a
    lot of filler leans packed, a clean section table with symbols and little filler leans normal.
    When neither holds the answer stays `inconclusive` rather than guessing.
    """
    if exec_entropy is None:
        return "no_exec_segment"
    high_fraction = high_windows / windows if windows else 0.0
    fill_fraction = fill_windows / windows if windows else 0.0
    if exec_entropy >= PACKED_ENTROPY or high_fraction >= HIGH_WINDOW_FRACTION:
        return "likely_packed_or_encrypted"
    if fill_fraction >= FILL_FRACTION_PACKED and high_fraction >= 0.05:
        return "likely_packed_or_encrypted"  # an encrypted chunk wrapped in filler
    if exec_entropy <= NORMAL_ENTROPY:
        return "normal"
    if section_table == "out_of_bounds" or fill_fraction >= FILL_FRACTION_HINT:
        return "likely_packed_or_encrypted"
    if section_table == "ok" and defined_symbols >= 1 and fill_fraction < 0.1 and high_fraction < 0.1:
        return "normal"
    return "inconclusive"


def inspect_elf(path: Path) -> ElfReport:
    size = path.stat().st_size
    if size == 0:
        raise ElfFormatError(f"{path} is empty")
    with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        try:
            lay = _parse_layout(buf)
            headers = _program_headers(buf, lay)
            symbols = _symbols(buf, lay)
        except ElfFormatError as exc:
            raise ElfFormatError(f"{path}: {exc}") from exc
        match = identify_packer(buf)

        segments: list[Segment] = []
        exec_counts: Counter[int] = Counter()
        exec_bytes = windows = high = fill = 0
        for index, ph in enumerate(headers):
            if ph.type != PT_LOAD:
                continue
            entropy = None
            if ph.flags & PF_X and ph.file_size:
                exec_bytes += ph.file_size
                if exec_bytes > size:
                    raise ElfFormatError(f"{path}: executable segments overlap (scan exceeds file size)")
                stats = _window_stats(buf, ph.offset, ph.file_size)
                exec_counts.update(stats.counts)
                windows, high, fill = windows + stats.windows, high + stats.high, fill + stats.fill
                entropy = round(shannon_entropy(stats.counts, ph.file_size), 4)
            segments.append(Segment(index, _flags(ph.flags), ph.offset, ph.file_size, ph.vaddr, ph.mem_size, entropy))

    exec_entropy = round(shannon_entropy(exec_counts, exec_bytes), 4) if exec_bytes else None
    return ElfReport(
        path=str(path),
        size=size,
        elf_class=32 if lay.elf_class == 1 else 64,
        machine=_MACHINES.get(lay.machine, f"unknown({lay.machine})"),
        load_segments=tuple(segments),
        section_count=lay.shnum,
        symbols=symbols,
        exec_entropy=exec_entropy,
        exec_windows=windows,
        high_entropy_windows=high,
        fill_windows=fill,
        packing_verdict=verdict(exec_entropy, windows, high, fill, symbols.section_table, symbols.defined or 0),
        packer=match.name if match else None,
    )
