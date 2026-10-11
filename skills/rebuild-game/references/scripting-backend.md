# Script evidence and behavioral reconstruction

## Mono

Decompile available game assemblies with `mono-decompile`. Decompiled C# is an
approximation of compiled behavior, not the original source project. Obfuscation,
stripping, dependencies, generated code and build configuration can prevent
successful reconstruction. Confirm script identity using assembly, namespace and
class, then verify compilation and serialized field compatibility in Unity.

Do not change network/authentication/save methods to apparent success just to
make the project compile. Record absent behavior and explicitly test any chosen
replacement.

## IL2CPP

| Evidence | Command | Interpretation |
|---|---|---|
| Type and member names | `dump-il2cpp` | Identity/search evidence; no method bodies |
| Signatures and addresses | `il2cpp-enrich` | Helps map metadata to native functions |
| Native pseudo-C | `decompile` | Code analysis evidence; not drop-in C# |
| Annotated type files | `il2cpp-annotate` | Documentation under Reconstruction/CodeEvidence |

Use `blueprint.script_inventory` to connect MonoScripts with candidate metadata
members. Namespace/class matches can be ambiguous across assemblies; the report
does not assert execution. Inspect concrete methods, config fields, event targets
and state transitions. Save interpretations with workdir-local line citations in
`behavior.json`; use `inference` when the behavior is not directly established.

## Core behavior checklist

Trace startup and persistent objects, screen/state transitions, level loading,
input, gameplay rules, win/loss, rewards, progression, persistence and service
interactions. Separate physical Unity scenes from screens instantiated at runtime.
Pair static analysis with reproducible observations when a runnable game is
available. Mark unresolved systems unknown and explain the missing evidence.

## SDKs and packages

Signatures in MonoScript records, metadata names and library filenames identify
presence. They do not prove runtime initialization or original package versions.
Inspect call sites and configuration before describing a service's role. Do not
publish secrets from configuration or use live services without authorization.
