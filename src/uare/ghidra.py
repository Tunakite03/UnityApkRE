"""Opt-in native decompilation through Ghidra's headless analyzer.

`uare decompile <so>` runs Ghidra's `analyzeHeadless` on a native library (typically the unpacked
`libil2cpp.so`) with a bundled post-script that exports the function list and, optionally, the
C decompilation of every function. The addresses line up with the RVAs from `uare il2cpp-enrich`,
so a method name can be joined to its pseudo-C.

Ghidra is not bundled. Point at it with `--ghidra`, `UARE_GHIDRA`, a vendored copy under
vendor/ghidra, or `analyzeHeadless` on PATH; `uare fetch-tool` can download the release archive.
The post-script is Jython (Ghidra 9/10, or 11 with the Jython extension); this module writes it on
demand, so nothing is executed without the user asking.
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.ghidra")

DEFAULT_VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "ghidra"
_HEADLESS_NAMES = ("analyzeHeadless.bat", "analyzeHeadless", "support/analyzeHeadless", "support/analyzeHeadless.bat")
_PROJECT_NAME = "uare_headless"
_MAX_STDERR = 800

# Jython post-script run by analyzeHeadless. getScriptArgs()[0] is the output directory;
# getScriptArgs()[1], when present, is "decompile".
_SCRIPT = '''# uare Ghidra export: function list + optional C decompilation.
import os
from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor

args = getScriptArgs()
out_dir = args[0]
do_decompile = len(args) > 1 and args[1] == "decompile"
fm = currentProgram.getFunctionManager()
monitor = ConsoleTaskMonitor()
decomp = DecompInterface()
decomp.openProgram(currentProgram) if do_decompile else None

with open(os.path.join(out_dir, "functions.tsv"), "w") as listing:
    for func in fm.getFunctions(True):
        entry = func.getEntryPoint()
        name = func.getName()
        size = func.getBody().getNumAddresses()
        listing.write("%s\\t%s\\t%d\\n" % (entry, name, size))
        if not do_decompile:
            continue
        try:
            res = decomp.decompileFunction(func, 30, monitor)
            if res and res.decompileCompleted():
                safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:80]
                path = os.path.join(out_dir, "decompiled", "%s_%s.c" % (entry, safe))
                with open(path, "w") as cfile:
                    cfile.write(res.getDecompiledFunction().getC())
        except Exception as exc:
            print("uare: decompile failed for %s: %s" % (entry, exc))
'''


class GhidraError(UareError):
    """Ghidra could not be located or run, or its output could not be read."""


@dataclass(frozen=True)
class JavaFunction:
    address: int
    name: str
    size: int


@dataclass(frozen=True)
class GhidraResult:
    ghidra: str
    functions: int
    decompiled: int
    out_dir: str


def _usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_ghidra(explicit: str | None, environ: Mapping[str, str], vendor_root: Path,
                   which: Callable[[str], str | None]) -> str:
    if explicit is not None:
        if not _usable(Path(explicit)):
            raise GhidraError(f"--ghidra {explicit} is not an executable file")
        return explicit
    env_value = environ.get("UARE_GHIDRA")
    if env_value:
        if not _usable(Path(env_value)):
            raise GhidraError(f"UARE_GHIDRA={env_value} is not an executable file")
        return env_value
    for name in _HEADLESS_NAMES:
        bundled = vendor_root / name
        if _usable(bundled):
            return str(bundled)
    for name in _HEADLESS_NAMES:
        found = which(name)
        if found:
            return found
    raise GhidraError(
        "analyzeHeadless not found: pass --ghidra, set UARE_GHIDRA, vendor Ghidra with `uare fetch-tool`, "
        "or put it on PATH"
    )


def write_script(script_dir: Path) -> Path:
    script_dir.mkdir(parents=True, exist_ok=True)
    path = script_dir / "uare_export.py"
    path.write_text(_SCRIPT, encoding="utf-8")
    return path


def build_command(ghidra: str, project_dir: Path, so: Path, script: Path, out_dir: Path,
                  decompile: bool) -> list[str]:
    argv = [ghidra, str(project_dir), _PROJECT_NAME, "-import", str(so),
            "-scriptPath", str(script.parent), "-postScript", script.name, str(out_dir)]
    if decompile:
        argv.append("decompile")
    argv += ["-deleteProject"]
    return argv


def parse_functions(path: Path) -> list[JavaFunction]:
    functions: list[JavaFunction] = []
    if not path.is_file():
        return functions
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            address = int(parts[0], 16)
            size = int(parts[2])
        except ValueError:
            continue
        functions.append(JavaFunction(address=address, name=parts[1], size=size))
    return functions


def decompile(so: Path, out_dir: Path, ghidra: str, *, decompile_functions: bool = True,
              timeout: float = 7200.0, project_dir: Path | None = None) -> GhidraResult:
    if not so.is_file():
        raise GhidraError(f"{so} not found")
    with staged_output(out_dir) as staging:
        script = write_script(staging / "scripts")
        project = project_dir or (staging / "project")
        project.mkdir(parents=True, exist_ok=True)
        argv = build_command(ghidra, project, so, script, staging, decompile_functions)
        event(LOG, logging.INFO, "ghidra_run", so=str(so), decompile=decompile_functions)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GhidraError(f"could not run Ghidra: {exc}") from exc
        if proc.returncode != 0:
            raise GhidraError(f"analyzeHeadless exited {proc.returncode}: {(proc.stderr or proc.stdout)[-_MAX_STDERR:]}")
        functions = parse_functions(staging / "functions.tsv")
        decompiled = len(list((staging / "decompiled").glob("*.c"))) if (staging / "decompiled").is_dir() else 0
    return GhidraResult(ghidra=ghidra, functions=len(functions), decompiled=decompiled, out_dir=str(out_dir))


def summarize(result: GhidraResult) -> list[str]:
    return [
        f"ghidra            : {result.ghidra}",
        f"functions         : {result.functions} -> functions.tsv",
        f"decompiled        : {result.decompiled} -> decompiled/*.c",
        f"output            : {result.out_dir}",
    ]
