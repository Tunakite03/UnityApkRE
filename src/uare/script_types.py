"""MonoBehaviour type trees generated from supplied managed assemblies.

IL2CPP builds usually ship without type trees, so script fields are opaque bytes. When the managed
assembly of a script is available (UGUI, TextMeshPro, tween or UI-effect packages, an Editor's
engine modules), its serialized layout can be generated from the DLL. UnityPy reads every object with
an exact byte-consumption check, so a layout from a different package version fails that check and the
object keeps its header-only fallback instead of being mis-read.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uare.errors import UareError

_ALIGN = 0x4000  # TypeTreeNode meta flag: align the stream to 4 bytes after this field


class ScriptTypesError(UareError):
    """Script type trees cannot be generated from the supplied assemblies."""


@dataclass(frozen=True)
class AssemblySource:
    name: str
    path: str
    sha256: str


@dataclass(frozen=True)
class ScriptTypes:
    """A ready generator plus the provenance of every assembly it was given."""

    generator: Any
    unity_version: str
    loaded: tuple[AssemblySource, ...]
    rejected: tuple[str, ...]


def collect_assemblies(directories: Sequence[Path]) -> tuple[list[Path], list[str]]:
    """Every *.dll under the directories, first occurrence of an assembly name wins (argument order)."""
    chosen: dict[str, Path] = {}
    shadowed: list[str] = []
    for directory in directories:
        if not directory.is_dir():
            raise ScriptTypesError(f"Script assembly directory does not exist; path={directory}")
        for path in sorted(directory.rglob("*.dll")):
            key = path.name.lower()
            if key in chosen:
                shadowed.append(f"{path} (shadowed by {chosen[key]})")
            else:
                chosen[key] = path
    if not chosen:
        raise ScriptTypesError(f"No .dll files under the script assembly directories; paths={list(map(str, directories))}")
    return list(chosen.values()), shadowed


def align_monobehaviour_header(entries: list[Any]) -> list[Any]:
    """Generated MonoBehaviour roots leave m_Enabled unaligned; Unity serializes it as UInt8 + align 4.

    Patched on the generator's flat entries: UnityPy's C reader reads flags when it builds its nodes,
    so changing an already built node has no effect on reads.
    """
    for entry in entries:
        if entry.m_Level == 1 and entry.m_Name == "m_Enabled":
            entry.m_MetaFlag |= _ALIGN
    return entries


@contextmanager
def _native_stdout_silenced() -> Iterator[None]:
    """The native generator prints resolution errors to fd 1; the caller reports failures itself."""
    sys.stdout.flush()
    saved = os.dup(1)
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        try:
            yield
        finally:
            os.dup2(saved, 1)
            os.close(saved)


def _generator_class() -> type:
    try:
        from TypeTreeGeneratorAPI import TypeTreeGenerator as _Native  # noqa: F401  (availability check)
        from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
    except ImportError as exc:
        raise ScriptTypesError("Script type trees need the TypeTreeGeneratorAPI package; "
                               "install the project's `power` extra") from exc

    class AlignedGenerator(TypeTreeGenerator):
        """UnityPy's generator with the m_Enabled alignment fix and cached failures."""

        def __init__(self, unity_version: str) -> None:
            super().__init__(unity_version)
            self.failed: dict[tuple[str, str], str] = {}

        def get_nodes(self, assembly: str, fullname: str) -> list[Any]:
            return align_monobehaviour_header(super().get_nodes(assembly, fullname))

        def get_nodes_up(self, assembly: str, fullname: str) -> Any:
            key = (assembly, fullname)
            if key in self.cache:
                return self.cache[key]
            if key in self.failed:
                raise ValueError(self.failed[key])
            try:
                with _native_stdout_silenced():
                    node = super().get_nodes_up(assembly, fullname)
            except AssertionError:
                # TypeTreeGeneratorAPI asserts when the assembly or a referenced type is unavailable.
                self.failed[key] = f"No generated type tree; assembly={assembly}, class={fullname}"
                raise ValueError(self.failed[key]) from None
            return node

    return AlignedGenerator


def load_script_types(unity_version: str, directories: Sequence[Path]) -> ScriptTypes:
    """Load every assembly under `directories` into one generator for `unity_version`."""
    generator = _generator_class()(unity_version)
    paths, rejected = collect_assemblies(directories)
    loaded: list[AssemblySource] = []
    for path in paths:
        payload = path.read_bytes()
        try:
            with _native_stdout_silenced():
                generator.load_dll(payload)
        except AssertionError:
            rejected.append(f"{path} (not a loadable managed assembly)")
            continue
        loaded.append(AssemblySource(path.name, str(path), hashlib.sha256(payload).hexdigest()))
    if not loaded:
        raise ScriptTypesError(f"No supplied assembly could be loaded; paths={list(map(str, directories))}")
    return ScriptTypes(generator, unity_version, tuple(loaded), tuple(rejected))
