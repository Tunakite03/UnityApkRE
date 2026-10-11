"""Opt-in native decompilation through Ghidra's headless analyzer.

`uare decompile <so>` runs Ghidra's `analyzeHeadless` on a native library (typically the unpacked
`libil2cpp.so`) with a bundled post-script that exports the function list and, optionally, the
C decompilation of every function. The addresses line up with the RVAs from `uare il2cpp-enrich`,
so a method name can be joined to its pseudo-C.

Point at Ghidra with `--ghidra`, `UARE_GHIDRA`, a local copy under vendor/ghidra,
or `analyzeHeadless` on PATH. The Java post-script works without a Jython extension.
See docs/agent-tools.md for the local distribution and JDK launcher.
"""

import logging
import os
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Mapping

from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.ghidra")

DEFAULT_VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "ghidra"
_HEADLESS_NAMES = ("analyzeHeadless.bat", "analyzeHeadless", "support/analyzeHeadless", "support/analyzeHeadless.bat")
_PROJECT_NAME = "uare_headless"

# Java post-script: arguments are the output directory and optionally "decompile".
_SCRIPT = r'''// @category UARE
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

public class UareExport extends GhidraScript {
    @Override
    protected void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 1 || args.length > 2 ||
                (args.length == 2 && !args[1].equals("decompile"))) {
            throw new IllegalArgumentException("Usage: UareExport.java <output> [decompile]");
        }
        Path output = Path.of(args[0]);
        Files.createDirectories(output);
        boolean exportCode = args.length == 2;
        DecompInterface decompiler = new DecompInterface();
        try {
            if (exportCode) {
                Files.createDirectories(output.resolve("decompiled"));
                if (!decompiler.openProgram(currentProgram)) {
                    throw new IOException("Cannot open program: " + decompiler.getLastMessage());
                }
            }
            try (BufferedWriter listing = Files.newBufferedWriter(
                    output.resolve("functions.tsv"), StandardCharsets.UTF_8)) {
                FunctionIterator functions = currentProgram.getFunctionManager().getFunctions(true);
                while (functions.hasNext()) {
                    monitor.checkCancelled();
                    Function function = functions.next();
                    String address = function.getEntryPoint().toString();
                    String name = function.getName();
                    listing.write(address + "\t" + name + "\t" +
                            function.getBody().getNumAddresses() + "\n");
                    if (exportCode) {
                        DecompileResults result = decompiler.decompileFunction(function, 30, monitor);
                        if (!result.decompileCompleted() || result.getDecompiledFunction() == null) {
                            throw new IOException("Decompilation failed at " + address + ": " +
                                    result.getErrorMessage());
                        }
                        String safeName = name.replaceAll("[^a-zA-Z0-9._-]", "_");
                        safeName = safeName.substring(0, Math.min(80, safeName.length()));
                        Files.writeString(output.resolve("decompiled").resolve(
                                address + "_" + safeName + ".c"),
                                result.getDecompiledFunction().getC(), StandardCharsets.UTF_8);
                    }
                }
            }
            Files.writeString(output.resolve("export-complete.txt"), "complete\n",
                    StandardCharsets.UTF_8);
        } finally {
            decompiler.dispose();
        }
    }
}
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
    path = script_dir / "UareExport.java"
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
    with staged_output(out_dir) as staging, ExitStack() as cleanup:
        script = write_script(staging / "scripts")
        # Ghidra 12 rejects dot-prefixed project path components; staged outputs
        # are hidden, so keep the disposable analysis project outside staging.
        project = project_dir or Path(cleanup.enter_context(TemporaryDirectory(prefix="uare-ghidra-")))
        project.mkdir(parents=True, exist_ok=True)
        argv = build_command(ghidra, project, so, script, staging, decompile_functions)
        event(LOG, logging.INFO, "ghidra_run", so=str(so), decompile=decompile_functions)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GhidraError(f"could not run Ghidra: {exc}") from exc
        if proc.returncode != 0:
            raise GhidraError(
                f"analyzeHeadless exited {proc.returncode}: "
                + (proc.stderr + "\n" + proc.stdout)[-4000:]
            )
        # Ghidra may exit zero after a post-script error; require script completion.
        if not (staging / "export-complete.txt").is_file():
            raise GhidraError(
                "Ghidra export did not complete; post-script failed: "
                + (proc.stderr + "\n" + proc.stdout)[-4000:]
            )
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
