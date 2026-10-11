"""Decompile Mono/.NET assemblies to C# source using ILSpy (ilspycmd) or similar tools.

For Mono-backend Unity games, the managed DLLs under ``Managed/`` contain the full game logic
as standard .NET assemblies. Decompiling them recovers readable C# source, making Mono games
far easier to rebuild than IL2CPP ones.

This module follows the same tool-resolution pattern as ``ghidra.py``: explicit flag, env var,
vendored copy, then PATH. It runs ``ilspycmd`` (the ILSpy command-line interface) on each DLL
that is not a known Unity/system assembly, outputting a C# project per assembly.
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

LOG = logging.getLogger("uare.mono_decompile")

DEFAULT_VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "ilspy"
_DECOMPILER_NAMES = ("ilspycmd", "ilspycmd.exe", "ILSpy", "ILSpy.exe")
_MAX_STDERR = 800

_SYSTEM_ASSEMBLIES = frozenset({
    "mscorlib", "System", "System.Core", "System.Xml", "System.Xml.Linq",
    "System.Data", "System.Net.Http", "System.Numerics", "System.Runtime.Serialization",
    "System.IO.Compression", "System.IO.Compression.FileSystem",
    "System.ComponentModel.Composition", "System.Configuration",
    "System.Drawing", "System.EnterpriseServices", "System.ServiceModel",
    "System.Transactions", "System.Web", "System.Windows.Forms",
    "Mono.Cecil", "Mono.Security", "Mono.Posix",
    "netstandard", "Microsoft.CSharp",
})

_UNITY_ASSEMBLIES = frozenset({
    "UnityEngine", "UnityEngine.UI", "UnityEngine.CoreModule",
    "UnityEngine.PhysicsModule", "UnityEngine.AnimationModule",
    "UnityEngine.AudioModule", "UnityEngine.IMGUIModule",
    "UnityEngine.InputLegacyModule", "UnityEngine.ParticleSystemModule",
    "UnityEngine.Physics2DModule", "UnityEngine.TextRenderingModule",
    "UnityEngine.UIModule", "UnityEngine.UnityWebRequestModule",
    "UnityEngine.UnityWebRequestWWWModule",
    "UnityEngine.ImageConversionModule", "UnityEngine.JSONSerializeModule",
    "UnityEngine.AIModule", "UnityEngine.AssetBundleModule",
    "UnityEngine.Networking", "UnityEngine.TestRunner",
    "Unity.TextMeshPro", "Unity.Timeline", "Unity.Mathematics",
    "Unity.Burst", "Unity.Collections", "Unity.Jobs",
    "Unity.Entities", "Unity.Rendering.Hybrid", "Unity.Transforms",
})


class DecompilerError(UareError):
    """The decompiler could not be located or run."""


@dataclass(frozen=True)
class AssemblyResult:
    name: str
    path: str
    files: int
    error: str | None


@dataclass(frozen=True)
class MonoDecompileResult:
    decompiler: str
    managed_dir: str
    total_dlls: int
    decompiled: int
    skipped_system: int
    skipped_unity: int
    assemblies: tuple[AssemblyResult, ...]
    total_cs_files: int
    errors: int


def _is_game_assembly(stem: str) -> bool:
    if stem in _SYSTEM_ASSEMBLIES or stem in _UNITY_ASSEMBLIES:
        return False
    if stem.startswith("UnityEngine.") or stem.startswith("Unity."):
        return False
    if stem.startswith("System.") or stem.startswith("Microsoft."):
        return False
    if stem.startswith("Mono."):
        return False
    return True


def resolve_decompiler(explicit: str | None, environ: Mapping[str, str], vendor_root: Path,
                       which: Callable[[str], str | None]) -> str:
    if explicit is not None:
        p = Path(explicit)
        if not p.is_file():
            raise DecompilerError(f"--decompiler {explicit} is not a file")
        return explicit
    env_value = environ.get("UARE_ILSPY")
    if env_value:
        p = Path(env_value)
        if not p.is_file():
            raise DecompilerError(f"UARE_ILSPY={env_value} is not a file")
        return env_value
    for name in _DECOMPILER_NAMES:
        bundled = vendor_root / name
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    for name in _DECOMPILER_NAMES:
        found = which(name)
        if found:
            return found
    raise DecompilerError(
        "ilspycmd not found: pass --decompiler, set UARE_ILSPY, vendor ILSpy with "
        "`uare fetch-tool`, or put ilspycmd on PATH. "
        "Local install: `dotnet tool install ilspycmd --tool-path vendor/ilspy`"
    )


def decompile_assembly(dll: Path, out_dir: Path, decompiler: str, *,
                       timeout: float = 600.0) -> int:
    """Decompile one assembly into a directory of .cs files. Returns the count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [decompiler, str(dll), "-p", "-o", str(out_dir)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DecompilerError(f"could not run decompiler on {dll.name}: {exc}") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "")[-_MAX_STDERR:]
        raise DecompilerError(f"decompiler exited {proc.returncode} on {dll.name}: {stderr}")
    return sum(1 for _ in out_dir.rglob("*.cs"))


def decompile_all(managed_dir: Path, out_dir: Path, decompiler: str, *,
                  timeout: float = 600.0) -> MonoDecompileResult:
    """Decompile all game assemblies under a Managed/ directory."""
    if not managed_dir.is_dir():
        raise DecompilerError(f"{managed_dir} is not a directory")
    dlls = sorted(managed_dir.glob("*.dll"))
    results: list[AssemblyResult] = []
    decompiled = skipped_system = skipped_unity = errors = total_cs = 0
    with staged_output(out_dir) as staging:
        for dll in dlls:
            stem = dll.stem
            if stem in _SYSTEM_ASSEMBLIES:
                skipped_system += 1
                continue
            if stem in _UNITY_ASSEMBLIES or stem.startswith("UnityEngine.") or stem.startswith("Unity."):
                skipped_unity += 1
                continue
            if stem.startswith("System.") or stem.startswith("Microsoft.") or stem.startswith("Mono."):
                skipped_system += 1
                continue
            assembly_dir = staging / stem
            try:
                count = decompile_assembly(dll, assembly_dir, decompiler, timeout=timeout)
                results.append(AssemblyResult(stem, str(dll), count, None))
                decompiled += 1
                total_cs += count
                event(LOG, logging.INFO, "assembly_decompiled", assembly=stem, files=count)
            except DecompilerError as exc:
                results.append(AssemblyResult(stem, str(dll), 0, str(exc)))
                errors += 1
                event(LOG, logging.WARNING, "assembly_decompile_failed", assembly=stem, error=str(exc))
    return MonoDecompileResult(
        decompiler=decompiler, managed_dir=str(managed_dir), total_dlls=len(dlls),
        decompiled=decompiled, skipped_system=skipped_system, skipped_unity=skipped_unity,
        assemblies=tuple(results), total_cs_files=total_cs, errors=errors)


def summarize(result: MonoDecompileResult) -> list[str]:
    return [
        f"decompiler        : {result.decompiler}",
        f"managed dir       : {result.managed_dir}",
        f"total DLLs        : {result.total_dlls}",
        f"decompiled        : {result.decompiled} assemblies, {result.total_cs_files} .cs files",
        f"skipped           : {result.skipped_system} system, {result.skipped_unity} Unity",
        f"errors            : {result.errors}",
    ]
