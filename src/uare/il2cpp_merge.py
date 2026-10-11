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

_SANITIZE = re.compile(r"[^A-Za-z0-9_]")
# Assemblies the Unity Editor (or its packages) already provides. Stubs for their types would
# shadow the real ones (a stub `System.Action` is not a delegate, a stub `MonoBehaviour` is not a
# component), so they are never emitted.
_ENGINE_ASSEMBLY = re.compile(
    r"^(mscorlib|netstandard|System|System\..+|Mono\..+|Microsoft\..+|"
    r"UnityEngine|UnityEngine\..+|Unity\..+|__Generated)(\.dll)?$")
# Types the C# compiler (`<>c`, `<Foo>d__3`, `<PrivateImplementationDetails>`, array-init blobs) or
# Unity's build (MonoScript/Jobs source generators) emit into every assembly. They hold no
# authored code, and Unity regenerates its own copy, so a stub would be a duplicate definition.
_GENERATED_TYPE = re.compile(
    r"^(<|__StaticArrayInitTypeSize=|UnitySourceGenerated|__JobReflectionRegistrationOutput__)")


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
    engine_types_skipped: int
    generated_types_skipped: int
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


def _engine_type_indices(il2cpp_dir: Path) -> frozenset[int]:
    """Type indices that belong to engine/BCL assemblies, from images.tsv."""
    path = il2cpp_dir / "images.tsv"
    if not path.is_file():
        raise UareError(
            f"{path} not found; re-run `uare dump-il2cpp` (images.tsv tells engine types apart from "
            f"game types, without it the stubs would shadow Unity's own assemblies)")
    engine: set[int] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        if _ENGINE_ASSEMBLY.match(parts[1]):
            start, count = int(parts[2]), int(parts[3])
            engine.update(range(start, start + count))
    return frozenset(engine)


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


_CS_KEYWORDS = frozenset({
    "abstract", "as", "base", "bool", "break", "byte", "case", "catch", "char", "checked",
    "class", "const", "continue", "decimal", "default", "delegate", "do", "double", "else",
    "enum", "event", "explicit", "extern", "false", "finally", "fixed", "float", "for",
    "foreach", "goto", "if", "implicit", "in", "int", "interface", "internal", "is", "lock",
    "long", "namespace", "new", "null", "object", "operator", "out", "override", "params",
    "private", "protected", "public", "readonly", "ref", "return", "sbyte", "sealed",
    "short", "sizeof", "stackalloc", "static", "string", "struct", "switch", "this", "throw",
    "true", "try", "typeof", "uint", "ulong", "unchecked", "unsafe", "ushort", "using",
    "virtual", "void", "volatile", "while",
})


def _bare(name: str) -> str:
    """Identifier text without the verbatim ``@`` (``@try`` and ``try`` are the same name)."""
    return name[1:] if name.startswith("@") else name


def _escape_cs(name: str) -> str:
    """Turn an arbitrary IL2CPP name into one valid C# identifier."""
    if not name:
        return "_unnamed"
    ident = _SANITIZE.sub("_", name)
    if ident[0].isdigit():
        ident = f"_{ident}"
    if ident in _CS_KEYWORDS:
        ident = f"@{ident}"
    return ident


def _escape_namespace(namespace: str) -> str:
    return ".".join(_escape_cs(part) for part in namespace.split("."))


def _member_name(name: str) -> str:
    """Drop an explicit-interface prefix (``System.Collections.ICollection.Count`` -> ``Count``)."""
    return _escape_cs(name.rsplit(".", 1)[-1] if "." in name else name)


def _claim(ident: str, used: set[str]) -> str:
    """Reserve ``ident`` in ``used``, suffixing ``_1``, ``_2``... on collision."""
    candidate = ident
    counter = 1
    while _bare(candidate) in used:
        candidate = f"{_bare(ident)}_{counter}"
        counter += 1
    used.add(_bare(candidate))
    return candidate


def _render_body(lines: list[str], method: MergedMethod, indent: str) -> None:
    if method.pseudo_c:
        for c_line in method.pseudo_c.splitlines():
            lines.append(f"{indent}// {c_line}")


def _render_type(merged: MergedType, safe_class: str) -> str:
    """Render one stub; ``safe_class`` is the already-escaped, namespace-unique class name."""
    # A member may not share its enclosing type's name (CS0542).
    used: set[str] = {_bare(safe_class)}
    accessors: dict[str, str] = {}

    prop_names = [_claim(_member_name(p.name), used) for p in merged.properties]
    for name in prop_names:
        for prefix in ("get_", "set_"):
            accessors[f"{prefix}{_bare(name)}"] = name
    event_names = [_claim(_member_name(e.name), used) for e in merged.events]
    for name in event_names:
        for prefix in ("add_", "remove_"):
            accessors[f"{prefix}{_bare(name)}"] = name
    used.update(accessors)
    field_names = [_claim(_member_name(f.name), used) for f in merged.fields]

    lines: list[str] = [f"// Type index: {merged.index}"]
    if merged.namespace:
        lines.append(f"namespace {_escape_namespace(merged.namespace)}")
        lines.append("{")
    lines.append(f"public class {safe_class}")
    lines.append("{")

    for field, name in zip(merged.fields, field_names):
        lines.append(f"    // Field index: {field.index}")
        lines.append(f"    public object {name}; // type unknown without binary")
        lines.append("")

    for prop, name in zip(merged.properties, prop_names):
        lines.append(f"    // Property index: {prop.index}")
        lines.append(f"    public object {name} {{ get; set; }}")
        lines.append("")

    for ev, name in zip(merged.events, event_names):
        lines.append(f"    // Event index: {ev.index}")
        lines.append(f"    public event global::System.Action {name};")
        lines.append("")

    has_ctor = has_cctor = False
    for method in merged.methods:
        rva_comment = f" // RVA: 0x{method.rva:X}" if method.rva else ""
        lines.append(f"    // Method index: {method.index}{rva_comment}")
        stripped = _bare(_member_name(method.name))
        if method.name == ".ctor" and not has_ctor:
            has_ctor = True
            header = f"    public {safe_class}()"
        elif method.name == ".cctor" and not has_cctor:
            has_cctor = True
            header = f"    static {safe_class}()"
        elif method.name in (".ctor", ".cctor") or stripped in accessors:
            # Stub signatures carry no parameters, so extra constructor overloads and
            # property/event accessors cannot be declared without a collision; keep them as notes.
            owner = accessors.get(stripped)
            note = f"accessor of {owner}" if owner else f"{method.name} overload"
            lines.append(f"    // {method.name} ({note})")
            _render_body(lines, method, "    ")
            lines.append("")
            continue
        else:
            header = f"    public void {_claim(_member_name(method.name), used)}()"
        lines.append(header)
        lines.append("    {")
        _render_body(lines, method, "        ")
        if not method.pseudo_c:
            lines.append("        throw new global::System.NotImplementedException();")
        lines.append("    }")
        lines.append("")

    lines.append("}")
    if merged.namespace:
        lines.append("}")
    return "\n".join(lines) + "\n"


def merge(il2cpp_dir: Path, enrich_dir: Path | None, ghidra_dir: Path | None,
          out_dir: Path) -> MergeResult:
    types = _load_types(il2cpp_dir)
    engine = _engine_type_indices(il2cpp_dir)
    members = _load_members(il2cpp_dir)
    rvas = _load_rvas(enrich_dir)
    ghidra = _load_ghidra_functions(ghidra_dir)

    methods_total = methods_with_rva = methods_with_c = files_written = generated_skipped = 0
    class_names: dict[str, set[str]] = {}

    with staged_output(out_dir) as staging:
        for type_idx, namespace, type_name in types:
            if not type_name or type_idx in engine:
                continue
            if _GENERATED_TYPE.match(type_name):
                generated_skipped += 1
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
            # Nested types (`<>c`, `Enumerator`) and case-only variants share a sanitized name in one
            # namespace; claim a unique class/file name (case-insensitive: macOS/Windows filesystems).
            taken = class_names.setdefault(ns_path.casefold(), set())
            base_name = _escape_cs(type_name)
            class_name, counter = base_name, 1
            while _bare(class_name).casefold() in taken:
                class_name = f"{_bare(base_name)}_{counter}"
                counter += 1
            taken.add(_bare(class_name).casefold())
            target = staging / ns_path / f"{_bare(class_name)}.cs"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_render_type(merged_type, class_name), encoding="utf-8")
            files_written += 1

        summary = {
            "types": len(types), "methods_total": methods_total,
            "methods_with_rva": methods_with_rva, "methods_with_pseudo_c": methods_with_c,
            "files_written": files_written, "engine_types_skipped": len(engine),
            "generated_types_skipped": generated_skipped,
            "sources": {
                "il2cpp": str(il2cpp_dir),
                "enrich": str(enrich_dir) if enrich_dir else None,
                "ghidra": str(ghidra_dir) if ghidra_dir else None,
            },
        }
        (staging / "merge_report.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    event(LOG, logging.INFO, "il2cpp_merge_done", types=len(types), methods=methods_total,
          with_rva=methods_with_rva, with_c=methods_with_c, files=files_written, engine_skipped=len(engine),
          generated_skipped=generated_skipped)
    return MergeResult(len(types), methods_total, methods_with_rva, methods_with_c,
                       files_written, len(engine), generated_skipped, str(out_dir))


def summarize(result: MergeResult) -> list[str]:
    return [
        f"types             : {result.types}",
        f"methods           : {result.methods_total} total, {result.methods_with_rva} with RVA, "
        f"{result.methods_with_pseudo_c} with pseudo-C",
        f"files written     : {result.files_written} .cs stubs",
        f"engine types      : {result.engine_types_skipped} skipped (provided by Unity)",
        f"generated types   : {result.generated_types_skipped} skipped (compiler/Unity build output)",
        f"output            : {result.out_dir}",
    ]
