"""Merge IL2CPP name tables with RVA data and Ghidra decompilation to produce annotated source.

Three data sources are combined when available:

1. **IL2CPP dump** (always): type/method/field names and member grouping from ``uare dump-il2cpp``.
2. **IL2CPP enrich** (optional): RVA-to-method mapping from ``uare il2cpp-enrich`` (needs
   Il2CppDumper/Inspector on an unpacked ``libil2cpp.so``).
3. **Ghidra decompile** (optional): pseudo-C function bodies from ``uare decompile``.

With all three, the output is one ``.cs`` stub per type, where each method body contains its
Ghidra pseudo-C as a comment block, and each method carries its RVA. With only the dump, the
output is a navigable stub tree (namespace/class/method names) — already more useful than a
flat ``dump.cs`` because it preserves the type hierarchy.
"""

import csv
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.il2cpp_merge")

_SANITIZE = re.compile(r"[^A-Za-z0-9_.]")


@dataclass(frozen=True)
class MergedMethod:
    index: int
    name: str
    rva: int | None
    ghidra_address: int | None
    pseudo_c: str | None


@dataclass(frozen=True)
class MergedField:
    index: int
    name: str


@dataclass(frozen=True)
class MergedProperty:
    index: int
    name: str


@dataclass(frozen=True)
class MergedEvent:
    index: int
    name: str


@dataclass(frozen=True)
class MergedType:
    index: int
    name: str
    namespace: str
    methods: tuple[MergedMethod, ...]
    fields: tuple[MergedField, ...]
    properties: tuple[MergedProperty, ...]
    events: tuple[MergedEvent, ...]


@dataclass(frozen=True)
class MergeResult:
    types: int
    methods_total: int
    methods_with_rva: int
    methods_with_pseudo_c: int
    files_written: int
    out_dir: str


def _load_members(il2cpp_dir: Path) -> dict[int, dict[str, list[tuple[int, str]]]]:
    """Parse members.tsv into {type_index: {kind: [(member_index, name)]}}."""
    path = il2cpp_dir / "members.tsv"
    if not path.is_file():
        return {}
    result: dict[int, dict[str, list[tuple[int, str]]]] = defaultdict(lambda: defaultdict(list))
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        type_idx = int(parts[0])
        kind = parts[2]
        member_idx = int(parts[3])
        member_name = parts[4]
        result[type_idx][kind].append((member_idx, member_name))
    return dict(result)


def _load_types(il2cpp_dir: Path) -> list[tuple[int, str, str]]:
    """Parse types.tsv into [(index, namespace, name)]."""
    path = il2cpp_dir / "types.tsv"
    if not path.is_file():
        raise UareError(f"{path} not found; run `uare dump-il2cpp` first")
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        result.append((int(parts[0]), parts[1], parts[2]))
    return result


def _load_rvas(enrich_dir: Path | None) -> dict[int, int]:
    """Parse methods_rva.tsv into {method_index: rva}."""
    if enrich_dir is None:
        return {}
    path = enrich_dir / "methods_rva.tsv"
    if not path.is_file():
        return {}
    result: dict[int, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            result[int(parts[0])] = int(parts[1], 0)
        except ValueError:
            continue
    return result


def _load_ghidra_functions(ghidra_dir: Path | None) -> dict[int, str]:
    """Load Ghidra decompiled C files into {address: pseudo_c}."""
    if ghidra_dir is None:
        return {}
    decompiled = ghidra_dir / "decompiled"
    if not decompiled.is_dir():
        return {}
    result: dict[int, str] = {}
    for c_file in sorted(decompiled.glob("*.c")):
        stem = c_file.stem
        addr_hex = stem.split("_", 1)[0]
        try:
            address = int(addr_hex, 16)
        except ValueError:
            continue
        result[address] = c_file.read_text(encoding="utf-8", errors="replace")
    return result


def _escape_cs(name: str) -> str:
    if not name:
        return "_unnamed"
    return _SANITIZE.sub("_", name)


def _render_type(merged: MergedType) -> str:
    lines: list[str] = []
    qualified = f"{merged.namespace}.{merged.name}" if merged.namespace else merged.name
    lines.append(f"// Type index: {merged.index}")
    if merged.namespace:
        lines.append(f"namespace {_escape_cs(merged.namespace)}")
        lines.append("{")
    lines.append(f"public class {_escape_cs(merged.name)}")
    lines.append("{")

    for field in merged.fields:
        lines.append(f"    // Field index: {field.index}")
        lines.append(f"    public object {_escape_cs(field.name)}; // type unknown without binary")
        lines.append("")

    for prop in merged.properties:
        lines.append(f"    // Property index: {prop.index}")
        lines.append(f"    public object {_escape_cs(prop.name)} {{ get; set; }}")
        lines.append("")

    for ev in merged.events:
        lines.append(f"    // Event index: {ev.index}")
        lines.append(f"    public event System.Action {_escape_cs(ev.name)};")
        lines.append("")

    for method in merged.methods:
        rva_comment = f" // RVA: 0x{method.rva:X}" if method.rva else ""
        lines.append(f"    // Method index: {method.index}{rva_comment}")
        lines.append(f"    public void {_escape_cs(method.name)}()")
        lines.append("    {")
        if method.pseudo_c:
            for c_line in method.pseudo_c.splitlines():
                lines.append(f"        // {c_line}")
        else:
            lines.append("        throw new System.NotImplementedException();")
        lines.append("    }")
        lines.append("")

    lines.append("}")
    if merged.namespace:
        lines.append("}")
    return "\n".join(lines) + "\n"


def merge(il2cpp_dir: Path, enrich_dir: Path | None, ghidra_dir: Path | None,
          out_dir: Path) -> MergeResult:
    types = _load_types(il2cpp_dir)
    members = _load_members(il2cpp_dir)
    rvas = _load_rvas(enrich_dir)
    ghidra = _load_ghidra_functions(ghidra_dir)

    methods_total = methods_with_rva = methods_with_c = files_written = 0

    with staged_output(out_dir) as staging:
        for type_idx, namespace, type_name in types:
            if not type_name:
                continue
            type_members = members.get(type_idx, {})
            merged_methods: list[MergedMethod] = []
            for method_idx, method_name in type_members.get("method", []):
                methods_total += 1
                rva = rvas.get(method_idx)
                pseudo_c = None
                ghidra_addr = None
                if rva and rva in ghidra:
                    pseudo_c = ghidra[rva]
                    ghidra_addr = rva
                    methods_with_c += 1
                if rva:
                    methods_with_rva += 1
                merged_methods.append(MergedMethod(method_idx, method_name, rva, ghidra_addr, pseudo_c))

            merged_fields = [MergedField(i, n) for i, n in type_members.get("field", [])]
            merged_props = [MergedProperty(i, n) for i, n in type_members.get("property", [])]
            merged_events = [MergedEvent(i, n) for i, n in type_members.get("event", [])]

            merged_type = MergedType(
                type_idx, type_name, namespace, tuple(merged_methods),
                tuple(merged_fields), tuple(merged_props), tuple(merged_events))

            ns_path = namespace.replace(".", "/") if namespace else "_global"
            safe_name = _escape_cs(type_name)
            target = staging / ns_path / f"{safe_name}.cs"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_render_type(merged_type), encoding="utf-8")
            files_written += 1

        summary = {
            "types": len(types), "methods_total": methods_total,
            "methods_with_rva": methods_with_rva, "methods_with_pseudo_c": methods_with_c,
            "files_written": files_written,
            "sources": {
                "il2cpp": str(il2cpp_dir),
                "enrich": str(enrich_dir) if enrich_dir else None,
                "ghidra": str(ghidra_dir) if ghidra_dir else None,
            },
        }
        (staging / "merge_report.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    event(LOG, logging.INFO, "il2cpp_merge_done", types=len(types), methods=methods_total,
          with_rva=methods_with_rva, with_c=methods_with_c, files=files_written)
    return MergeResult(len(types), methods_total, methods_with_rva, methods_with_c,
                       files_written, str(out_dir))


def summarize(result: MergeResult) -> list[str]:
    return [
        f"types             : {result.types}",
        f"methods           : {result.methods_total} total, {result.methods_with_rva} with RVA, "
        f"{result.methods_with_pseudo_c} with pseudo-C",
        f"files written     : {result.files_written} .cs stubs",
        f"output            : {result.out_dir}",
    ]
