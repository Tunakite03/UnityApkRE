"""Identify known Android native packers from in-file markers.

A high-entropy executable segment says a library is packed; these markers often say *which*
packer, which tells you which unpacker to reach for. Detection is a plain byte search for
vendor-specific strings, so it is a hint, not proof: a marker can appear for other reasons, and a
packer that strips its own markers will not be named. `packing_verdict` in the ELF report stays
the authority on whether the code is readable.
"""

from dataclasses import dataclass
from pathlib import Path

# Distinctive marker -> packer family. Kept short and specific to avoid false positives; a match
# is reported as "likely <name>", never as certainty.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"libmobisec", "SecNeo (mobisec)"),
    (b"libDexHelper", "Bangcle (DexHelper)"),
    (b"libjiagu", "360 Jiagu"),
    (b"libshella", "Tencent Legu"),
    (b"libshellx", "Tencent Legu"),
    (b"libexec.so", "Tencent Mung"),
    (b"libddog", "DingDong"),
    (b"libdcc", "DingDong"),
    (b"libnsa", "Nagain"),
    (b"libbaiduprotect", "Baidu Protect"),
    (b"libAPKProtect", "APKProtect"),
    (b"libnqshield", "NQ Shield"),
    (b"libSecShell", "SecShell"),
    (b"libkwscmm", "Kuaishou Protect"),
    (b"libtup.so", "Tencent TUP"),
    (b"libaries.so", "Alibaba Protector"),
)


@dataclass(frozen=True)
class PackerMatch:
    name: str
    marker: str


def identify(buf: "bytes | bytearray | mmap_like", limit: int = 0) -> PackerMatch | None:
    """Return the first known packer whose marker appears in `buf`, or None.

    `buf` may be `bytes`, `bytearray` or a `mmap` (anything with `.find(sub, start, end)`), so a
    whole library can be scanned without copying it.
    """
    region: "bytes | bytearray | mmap_like" = buf if hasattr(buf, "find") else bytes(buf)
    end = len(region) if limit <= 0 else min(limit, len(region))
    for marker, name in _SIGNATURES:
        if region.find(marker, 0, end) >= 0:
            return PackerMatch(name=name, marker=marker.decode("ascii", errors="replace"))
    return None


def identify_file(path: Path) -> PackerMatch | None:
    with path.open("rb") as handle:
        return identify(handle.read())
