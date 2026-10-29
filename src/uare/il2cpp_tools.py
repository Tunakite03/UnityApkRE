"""Opt-in IL2CPP enrichment through an external dumper (Il2CppDumper / Il2CppInspector).

The metadata-only dump (il2cpp_dump.py) yields names and member grouping, but no RVAs, no
signatures and no base/nested/interface types: those need the `Il2CppType[]` array that lives in
`libil2cpp.so`. This module runs a dumper on the (unpacked) binary + metadata and folds its output
back into the toolkit's data model:

- methods_rva.tsv   `rva<TAB>class<TAB>method<TAB>signature` (from Il2CppDumper's script.json)
- dump.cs           the dumper's generated C# (copied, for signatures/structure)
- il2cpp.h          the dumper's generated header (copied, when present)
- enrich_report.json tool, counts, files, source paths

The dumper is an executable called as `<dumper> <libil2cpp.so> <global-metadata.dat> <out_dir>`
(Il2CppDumper's CLI convention). Resolution order: `--dumper`, `UARE_IL2CPP_DUMPER`, vendored
`vendor/il2cpp-dumper/<name>`, then PATH. Nothing here bypasses protection; run it on a binary
whose `packing_verdict` is `normal`.
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from uare.analyze import resolve_layout
from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.il2cpp_tools")

DEFAULT_VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "il2cpp-dumper"
_DUMPER_NAMES = ("Il2CppDumper.exe", "Il2CppDumper", "Il2CppDumper.dll", "Il2CppInspector", "Il2CppInspector.exe")
_MAX_STDERR = 800


class Il2CppToolsError(UareError):
    """The external dumper could not be located, run, or understood."""


@dataclass(frozen=True)
class ScriptMethod:
    address: int
    class_name: str
    method: str
    signature: str


@dataclass(frozen=True)
class EnrichResult:
    tool: str
    methods: int
    dump_cs: bool
    header: bool
    source_so: str
    source_metadata: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedDumper:
    path: str
    source: str  # --dumper | UARE_IL2CPP_DUMPER | vendored | PATH


def _usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _split_qualified(name: str) -> tuple[str, str]:
    """Il2CppDumper names methods as `Namespace.Class$$Method`; split into (class, method)."""
    class_name, sep, method = name.partition("$$")
    if not sep:
        return "", name
    return class_name, method


def parse_script_json(path: Path) -> list[ScriptMethod]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Il2CppToolsError(f"{path} is not valid Il2CppDumper script.json: {exc}") from exc
    raw = payload.get("ScriptMethod") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise Il2CppToolsError(f"{path} has no ScriptMethod list")
    methods: list[ScriptMethod] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        address, name = entry.get("Address"), entry.get("Name")
        if not isinstance(address, int) or not isinstance(name, str):
            continue
        class_name, method = _split_qualified(name)
        signature = entry.get("Signature") if isinstance(entry.get("Signature"), str) else ""
        methods.append(ScriptMethod(address=address, class_name=class_name, method=method, signature=signature))
    return methods


def resolve_dumper(explicit: str | None, environ: Mapping[str, str], vendor_root: Path,
                   which: Callable[[str], str | None]) -> ResolvedDumper:
    if explicit is not None:
        if not _usable(Path(explicit)):
            raise Il2CppToolsError(f"--dumper {explicit} is not an executable file")
        return ResolvedDumper(explicit, "--dumper")
    env_value = environ.get("UARE_IL2CPP_DUMPER")
    if env_value:
        if not _usable(Path(env_value)):
            raise Il2CppToolsError(f"UARE_IL2CPP_DUMPER={env_value} is not an executable file")
        return ResolvedDumper(env_value, "UARE_IL2CPP_DUMPER")
    for name in _DUMPER_NAMES:
        bundled = vendor_root / name
        if _usable(bundled):
            return ResolvedDumper(str(bundled), "vendored")
    for name in _DUMPER_NAMES:
        found = which(name)
        if found:
            return ResolvedDumper(found, "PATH")
    raise Il2CppToolsError(
        "no IL2CPP dumper found: pass --dumper, set UARE_IL2CPP_DUMPER, vendor it with `uare fetch-tool`, "
        "or put Il2CppDumper on PATH"
    )


def run_dumper(dumper: str, so: Path, metadata: Path, work_dir: Path, timeout: float) -> str:
    """Run the dumper and return the tail of its console output (it reports most failures there)."""
    argv = [dumper, str(so), str(metadata), str(work_dir)]
    event(LOG, logging.INFO, "dumper_run", dumper=dumper, so=str(so))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Il2CppToolsError(f"could not run {dumper}: {exc}") from exc
    if proc.returncode != 0:
        raise Il2CppToolsError(f"{dumper} exited {proc.returncode}: {(proc.stderr or proc.stdout)[-_MAX_STDERR:]}")
    return ((proc.stdout or "") + (proc.stderr or ""))[-_MAX_STDERR:]


def _default_so(layout) -> Path | None:
    if layout.lib_dir is None:
        return None
    libs = sorted(layout.lib_dir.glob("*/libil2cpp.so"))
    return libs[0] if libs else None


def enrich(root: Path, out_dir: Path, dumper: ResolvedDumper, *, so: Path | None = None,
           metadata: Path | None = None, timeout: float = 1800.0) -> EnrichResult:
    layout = resolve_layout(root)
    so_path = so or _default_so(layout)
    if so_path is None or not so_path.is_file():
        raise Il2CppToolsError("no libil2cpp.so found under lib/<abi>/; pass --so")
    metadata_path = metadata or (layout.data_dir / "Managed" / "Metadata" / "global-metadata.dat")
    if not metadata_path.is_file():
        raise Il2CppToolsError(f"{metadata_path} not found; pass --metadata")

    with staged_output(out_dir) as staging:
        work = staging / "dumper"
        work.mkdir()
        console = run_dumper(dumper.path, so_path, metadata_path, work, timeout)

        # Il2CppDumper reports many failures on its console and still exits 0; an empty
        # method table is a failed dump, not a binary without methods.
        script = work / "script.json"
        if not script.is_file():
            raise Il2CppToolsError(f"{dumper.path} exited 0 but wrote no script.json: {console!r}")
        methods = parse_script_json(script)
        if not methods:
            raise Il2CppToolsError(f"{dumper.path} wrote a script.json without method addresses: {console!r}")
        with (staging / "methods_rva.tsv").open("w", encoding="utf-8") as handle:
            for method in methods:
                handle.write(f"0x{method.address:X}\t{method.class_name}\t{method.method}\t{method.signature}\n")

        written: list[str] = ["methods_rva.tsv"]
        dump_cs = work / "dump.cs"
        if dump_cs.is_file():
            (staging / "dump.cs").write_bytes(dump_cs.read_bytes())
            written.append("dump.cs")
        header = work / "il2cpp.h"
        if header.is_file():
            (staging / "il2cpp.h").write_bytes(header.read_bytes())
            written.append("il2cpp.h")

        result = EnrichResult(tool=dumper.path, methods=len(methods), dump_cs=dump_cs.is_file(),
                              header=header.is_file(), source_so=str(so_path),
                              source_metadata=str(metadata_path), files=tuple(written))
        (staging / "enrich_report.json").write_text(
            json.dumps({"tool": dumper.path, "tool_source": dumper.source, "methods": len(methods),
                        "dump_cs": result.dump_cs, "header": result.header, "source_so": str(so_path),
                        "source_metadata": str(metadata_path), "files": written}, indent=2), encoding="utf-8")
    event(LOG, logging.INFO, "enrich_done", methods=result.methods, dump_cs=result.dump_cs)
    return result


def summarize(result: EnrichResult) -> list[str]:
    return [
        f"dumper            : {result.tool}",
        f"source            : {result.source_so}",
        f"metadata          : {result.source_metadata}",
        f"methods with RVA  : {result.methods} -> methods_rva.tsv",
        f"dump.cs           : {'yes' if result.dump_cs else 'no'}",
        f"il2cpp.h          : {'yes' if result.header else 'no'}",
        f"files             : {', '.join(result.files)}",
    ]
