# report.json fields

`uare analyze` writes these files to the analysis directory:

| File | Content |
|---|---|
| `report.json` | Summary (below) |
| `files.jsonl` | One record per file under `Data/`: path, kind, size, format_version, unity_version, type_count, target_platform, type_tree, compression, error |
| `assets_files.jsonl` | The same, for `assets/` outside `bin/Data`: Addressables (`aa/`), StreamingAssets, asset packs |
| `il2cpp/identifiers.tsv` | `offset<TAB>name` for every printable entry in the metadata string heap |
| `il2cpp/string_literals.jsonl` | `{"index", "value"}` per C# string literal |

## Top level

- `scripting_backend`: one of the following.
  - `il2cpp`: `global-metadata.dat` or `libil2cpp.so` is present.
  - `mono`: `Data/Managed/*.dll` is present.
  - `unknown`: neither was found.
- `scripting_assemblies`: assembly names from `Data/ScriptingAssemblies.json`.
- `boot_config`: key/value pairs from `Data/boot.config`.

## inventory and assets_inventory

| Field | Meaning |
|---|---|
| `kinds` | Count per kind: `serialized` (SerializedFile: scenes, sharedassets, GUID-named per-asset files), `resource` (.resS/.resource streamed data), `bundle_unityfs`, `bundle_legacy`, `text`, `unknown` (custom formats, encrypted blobs, IL2CPP metadata) |
| `unity_versions` | Engine version string per serialized file. A minority version means content built by another editor, or a leftover |
| `target_platforms` | BuildTarget per serialized file, for example Android = 13 |
| `type_tree_files` | Files that embed type trees. 0 is normal for player builds and means MonoBehaviour fields need the script layout (from metadata or dumps) to decode |
| `guid_named_files` | Files named by a 32-hex asset GUID. Some builds store one asset per file this way |
| `scene_files` | `levelN` files in index order (build-settings order) |
| `bundle_compression` | Count of AssetBundles per compression: none, lzma, lz4, lz4hc |
| `parse_errors` | Files whose header failed validation. Each record's `error` explains why |

## il2cpp

- `status`:
  - `ok`: the metadata was read.
  - `absent`: the build has no metadata.
  - `error`: `detail` explains, for example a wrong magic, which means encrypted or custom metadata.
- `version`: metadata version. 24-29 covers Unity 2018-2021, 31 covers Unity 2022.2+/6.
- `identifiers`, `string_literals`: row counts of the two output tables.
- `non_utf8_literals`: literals written with backslash escapes because they are not valid UTF-8.

## native_libraries[].report

| Field | Meaning |
|---|---|
| `machine`, `elf_class` | Architecture (`aarch64`, `arm`, `x86_64`, `x86`) and 32/64-bit |
| `load_segments` | PT_LOAD segments with flags (`R-X` = code) and per-code-segment entropy |
| `symbols.section_table` | `ok`, `absent` (stripped) or `out_of_bounds` (corrupted or deliberately broken; a protection signal) |
| `symbols.defined` / `imported` | Dynamic symbol counts |
| `symbols.il2cpp_exports` | `il2cpp_*` functions the library defines. Present on `libil2cpp.so` only; imports are excluded |
| `exec_entropy` | Shannon entropy (bits/byte) over all code bytes. Unpacked aarch64 code is about 5.7-7.0 |
| `exec_windows`, `high_entropy_windows`, `fill_windows` | 1 MiB windows of code, how many are at or above 7.5 bits (encrypted or compressed), and how many are at or below 1.0 (filler) |
| `packing_verdict` | `likely_packed_or_encrypted` when entropy is at least 7.2, or at least 25% of windows are high entropy. `normal` when entropy is at most 7.0. `inconclusive` in between. `no_exec_segment` when there is no code segment |

A packed library can show low average entropy, because a large filler region dilutes it. The window counts expose that case, which is why the verdict uses them.
