"""Copy a remapped AssetRipper project into a Unity project, binding package scripts instead of stub assemblies.

AssetRipper exports every managed assembly of an IL2CPP build as a stub DLL (types and fields, no method bodies),
including assemblies the target project already has as packages (`UnityEngine.UI`, `Unity.TextMeshPro`,
`Unity.Timeline`, ...). Importing both breaks compilation, and binding scenes to the stubs leaves those components
without behavior. Unity references a class inside a DLL by `fileID = int32(MD4("s\\0\\0\\0" + namespace + name)[:4])`
and the DLL's GUID; a class in a package is referenced by its script's GUID with file ID 11500000. This module
computes the first for every script of the given package folders and rewrites references from the stub to the
package script while copying. The stub DLL is not copied when its assembly name matches a package assembly
definition. References whose type is not found in the package keep the stub GUID and are counted.

Two more repairs make the stubs loadable by the Editor. A stub assembly whose name matches a precompiled DLL of a
package (for example `Newtonsoft.Json`) is replaced by that DLL: references keep their file ID (both are DLLs) and
take the package DLL's GUID. Every other stub keeps its compiler-generated
`UnitySourceGeneratedAssemblyMonoScriptTypes_v1` type under a disabled name: the Editor invokes that type's
method while scanning assemblies, and a stub has no method body, which crashes the Editor. Stubs are also
imported with Auto Reference off (`isExplicitlyReferenced: 1`): two stubs may declare the same global type
(`ObjectPool<T>`), which otherwise breaks every package assembly that uses that name, and with the Editor platform
enabled so editor compilation sees them. AssetRipper's recovered `Assets/Scripts/Assembly-CSharp` sources get an
assembly definition that references the stubs and the packages explicitly (`RECOVERED_ASSEMBLY`); scripts keep their
GUIDs, so scene and prefab bindings are unaffected.

Nothing is overwritten: every target path is checked before the first write, an identical existing file is
skipped, and a differing one stops the import.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import struct
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from uare.errors import UareError
from uare.logs import event

LOG = logging.getLogger("uare.unity_import")

SCRIPT_FILE_ID = 11500000
_GUID = re.compile(r"^guid: ([0-9a-f]{32})$", re.M)
_REF = re.compile(rb"\{fileID: (-?\d+), guid: ([0-9a-f]{32}), type: 3\}")
_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
GENERATED_TYPE = b"UnitySourceGeneratedAssemblyMonoScriptTypes_v1\0"
# Same length, so the metadata string heap keeps every offset; the Editor no longer finds the type by name.
DISABLED_TYPE = b"X" + GENERATED_TYPE[1:]
RECOVERED_SCRIPTS = "Scripts/Assembly-CSharp"
RECOVERED_ASSEMBLY = "Assembly-CSharp-Recovered"
_EDITOR_DISABLED = re.compile(rb"(first:\n\s+Editor: Editor\n\s+second:\n\s+enabled: )0")


class UnityImportError(UareError):
    """The AssetRipper project cannot be imported into the Unity project as requested."""


@dataclass(frozen=True)
class UnityImportResult:
    files_copied: int
    files_identical: int
    stub_assemblies_replaced: tuple[str, ...]
    stub_dlls_replaced: tuple[str, ...]
    stubs_repaired: int
    references_rebound: int
    references_unresolved: dict[str, int]
    report: str


def md4(message: bytes) -> bytes:
    """RFC 1320 MD4 (not available from OpenSSL 3 by default); used only to reproduce Unity's script file IDs."""
    def rotate(value: int, shift: int) -> int:
        return ((value << shift) | (value >> (32 - shift))) & 0xFFFFFFFF

    length = len(message) * 8
    message += b"\x80" + b"\x00" * ((55 - len(message)) % 64) + struct.pack("<Q", length)
    a, b, c, d = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476
    rounds = (
        (lambda x, y, z: (x & y) | (~x & z), range(16), (3, 7, 11, 19), 0),
        (lambda x, y, z: (x & y) | (x & z) | (y & z), (0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15),
         (3, 5, 9, 13), 0x5A827999),
        (lambda x, y, z: x ^ y ^ z, (0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15), (3, 9, 11, 15), 0x6ED9EBA1),
    )
    for offset in range(0, len(message), 64):
        words = struct.unpack("<16I", message[offset:offset + 64])
        aa, bb, cc, dd = a, b, c, d
        for function, order, shifts, constant in rounds:
            for step, index in enumerate(order):
                value = (a + function(b, c, d) + words[index] + constant) & 0xFFFFFFFF
                a, b, c, d = d, rotate(value, shifts[step % 4]), b, c
        a, b, c, d = (a + aa) & 0xFFFFFFFF, (b + bb) & 0xFFFFFFFF, (c + cc) & 0xFFFFFFFF, (d + dd) & 0xFFFFFFFF
    return struct.pack("<4I", a, b, c, d)


def script_file_id(namespace: str, name: str) -> int:
    """File ID Unity gives a MonoBehaviour class inside a managed DLL."""
    return struct.unpack("<i", md4(b"s\0\0\0" + (namespace + name).encode("utf-8"))[:4])[0]


def _meta_guid(path: Path) -> str | None:
    meta = path.with_name(path.name + ".meta")
    match = _GUID.search(meta.read_text(encoding="utf-8", errors="replace")) if meta.is_file() else None
    return match[1] if match else None


def package_scripts(package_dirs: Sequence[Path]) -> dict[str, dict[int, str]]:
    """Assembly name -> {DLL file ID -> script GUID} for every script file of each assembly definition found.

    A script binds when its file declares a class named like the file (Unity's MonoScript rule).
    """
    assemblies: dict[str, dict[int, str]] = defaultdict(dict)
    for root in package_dirs:
        if not root.is_dir():
            raise UnityImportError(f"Package folder not found; path={root}")
        definitions = sorted(root.rglob("*.asmdef"))
        owners = {d.parent: json.loads(d.read_text(encoding="utf-8-sig")).get("name", "") for d in definitions}
        for script in root.rglob("*.cs"):
            owner = next((owners[p] for p in [script.parent, *script.parents] if p in owners), None)
            guid = _meta_guid(script)
            if not owner or guid is None:
                continue
            text = script.read_text(encoding="utf-8", errors="replace")
            if not re.search(rf"\b(class|struct)\s+{re.escape(script.stem)}\b", text):
                continue
            namespace = _NAMESPACE.search(text)
            assemblies[owner][script_file_id(namespace[1] if namespace else "", script.stem)] = guid
    return dict(assemblies)


def package_dlls(package_dirs: Sequence[Path]) -> dict[str, str]:
    """File name -> GUID of every precompiled DLL in the package folders (a non-AOT copy wins over an AOT one)."""
    found: dict[str, tuple[bool, str]] = {}
    for root in package_dirs:
        for dll in sorted(root.rglob("*.dll")):
            guid = _meta_guid(dll)
            aot = "AOT" in dll.relative_to(root).parts
            if guid and (dll.name not in found or (found[dll.name][0] and not aot)):
                found[dll.name] = (aot, guid)
    return {name: guid for name, (_, guid) in found.items()}


def repair_stub(data: bytes) -> bytes:
    """Disable the compiler-generated MonoScript type of a stub assembly (see module docstring)."""
    return data.replace(GENERATED_TYPE, DISABLED_TYPE)


def unity_import(source_project: Path, unity_project: Path, package_dirs: Sequence[Path]) -> UnityImportResult:
    source, target = source_project / "Assets", unity_project / "Assets"
    if not source.is_dir():
        raise UnityImportError(f"Not a project folder with Assets/; path={source_project}")
    if not (unity_project / "ProjectSettings" / "ProjectVersion.txt").is_file():
        raise UnityImportError(f"Not a Unity project (no ProjectSettings/ProjectVersion.txt); path={unity_project}")
    packages = package_scripts(package_dirs)
    precompiled = package_dlls(package_dirs)
    stubs: dict[str, dict[int, str]] = {}
    dll_guids: dict[str, str] = {}
    skipped: set[Path] = set()
    replaced: list[str] = []
    replaced_dlls: list[str] = []
    for dll in sorted(source.rglob("*.dll")):
        guid = _meta_guid(dll)
        if guid and dll.stem in packages:
            stubs[guid] = packages[dll.stem]
            replaced.append(dll.stem)
        elif guid and dll.name in precompiled:
            dll_guids[guid] = precompiled[dll.name]
            replaced_dlls.append(dll.stem)
        else:
            continue
        skipped.update({dll, dll.with_name(dll.name + ".meta")})
    files = [p for p in sorted(source.rglob("*")) if p.is_file() and p not in skipped]
    recovered = source / RECOVERED_SCRIPTS
    definition: tuple[Path, bytes] | None = None
    if recovered.is_dir() and any(recovered.rglob("*.cs")) and not any(recovered.rglob("*.asmdef")):
        kept = sorted(p.name for p in source.rglob("*.dll") if p not in skipped)
        definition = (target / RECOVERED_SCRIPTS / f"{RECOVERED_ASSEMBLY}.asmdef", (json.dumps({
            "name": RECOVERED_ASSEMBLY, "references": sorted(replaced), "includePlatforms": [], "excludePlatforms": [],
            "allowUnsafeCode": True, "overrideReferences": True,
            "precompiledReferences": kept + sorted(f"{name}.dll" for name in replaced_dlls),
            "autoReferenced": True, "defineConstraints": [], "versionDefines": [], "noEngineReferences": False},
            indent=4) + "\n").encode())
        if definition[0].exists() and definition[0].read_bytes() != definition[1]:
            raise UnityImportError(f"Target file exists and differs; nothing was written; path={definition[0]}")
    identical = 0
    plan: list[tuple[Path, Path]] = []
    for path in files:
        if path.is_symlink():
            raise UnityImportError(f"Source project contains a symlink; path={path}")
        out = target / path.relative_to(source)
        if out.exists():
            # Rewritten text cannot be compared byte for byte before rewriting; compare after the same rewrite.
            if out.read_bytes() == _converted(path, stubs, dll_guids, Counter()):
                identical += 1
                continue
            raise UnityImportError(f"Target file exists and differs; nothing was written; path={out}")
        plan.append((path, out))
    rebound = repaired = 0
    unresolved: Counter[str] = Counter()
    for path, out in plan:
        out.parent.mkdir(parents=True, exist_ok=True)
        counts: Counter[str] = Counter()
        data = _converted(path, stubs, dll_guids, counts)
        if data is None:
            shutil.copy2(path, out)
            continue
        out.write_bytes(data)
        rebound += counts.pop("_rebound", 0)
        repaired += counts.pop("_repaired", 0)
        unresolved.update(counts)
    if definition is not None and not definition[0].exists():
        definition[0].parent.mkdir(parents=True, exist_ok=True)
        definition[0].write_bytes(definition[1])
    report = unity_project / "Logs" / "uare-import.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    result = UnityImportResult(len(plan), identical, tuple(replaced), tuple(replaced_dlls), repaired, rebound,
                               dict(unresolved), str(report))
    report.write_text(json.dumps({**asdict(result), "source": str(source_project),
                                  "packages": [str(p) for p in package_dirs]}, indent=2) + "\n", encoding="utf-8")
    event(LOG, logging.INFO, "unity_import_done", copied=len(plan), identical=identical, stubs=len(replaced),
          rebound=rebound, unresolved=sum(unresolved.values()))
    return result


def _converted(path: Path, stubs: dict[str, dict[int, str]], dll_guids: dict[str, str],
               counts: Counter[str]) -> bytes | None:
    """The bytes to write for `path`, or None when it is copied unchanged."""
    data = path.read_bytes()
    if path.suffix == ".dll":
        if GENERATED_TYPE not in data:
            return None
        counts["_repaired"] += 1
        return repair_stub(data)
    if path.name.endswith(".dll.meta"):
        data = data.replace(b"\n  isExplicitlyReferenced: 0\n", b"\n  isExplicitlyReferenced: 1\n")
        return _EDITOR_DISABLED.sub(rb"\g<1>1", data)
    if b"\0" in data[:8192] or b"guid: " not in data:
        return None
    return _rewritten(data, stubs, dll_guids, counts)


def _rewritten(data: bytes, stubs: dict[str, dict[int, str]], dll_guids: dict[str, str],
               counts: Counter[str]) -> bytes:
    if not (stubs or dll_guids) or b"guid: " not in data:
        return data

    def swap(match: re.Match[bytes]) -> bytes:
        guid = match[2].decode()
        if guid in dll_guids:
            counts["_rebound"] += 1
            return b"{fileID: %s, guid: %s, type: 3}" % (match[1], dll_guids[guid].encode())
        types = stubs.get(guid)
        if types is None:
            return match[0]
        script = types.get(int(match[1]))
        if script is None:
            counts[f"{match[2].decode()}:{int(match[1])}"] += 1
            return match[0]
        counts["_rebound"] += 1
        return b"{fileID: %d, guid: %s, type: 3}" % (SCRIPT_FILE_ID, script.encode())

    return _REF.sub(swap, data)


def summarize(result: UnityImportResult) -> list[str]:
    return [f"files copied      : {result.files_copied} ({result.files_identical} identical files already present)",
            f"stub assemblies   : {', '.join(result.stub_assemblies_replaced) or 'none'} replaced by package scripts; "
            f"{', '.join(result.stub_dlls_replaced) or 'none'} replaced by package DLLs",
            f"stubs repaired    : {result.stubs_repaired} (generated MonoScript type disabled)",
            f"script references : {result.references_rebound} rebound, {sum(result.references_unresolved.values())} "
            f"left on a stub type not found in the packages",
            f"report            : {result.report}"]
