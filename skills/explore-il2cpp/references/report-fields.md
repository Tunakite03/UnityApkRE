# Output files

## `uare dump-il2cpp` (il2cpp/ directory)

| File | Content |
|---|---|
| `types.tsv` | `index<TAB>namespace<TAB>name` for every type definition |
| `methods.tsv`, `fields.tsv`, `parameters.tsv`, `properties.tsv`, `events.tsv` | `index<TAB>name` per definition |
| `members.tsv` | `typeIndex<TAB>typeName<TAB>kind<TAB>memberIndex<TAB>memberName`, grouping methods/fields/events/properties under their declaring type |
| `sections.json` | metadata version, the detected layout name, and the section offsets/sizes (feed to other tools) |

Names are taken from each record's nameIndex. Unreadable (obfuscated/blob) entries are emitted as empty and counted in the command's summary. `members.tsv` is built from the per-type member ranges in `Il2CppTypeDefinition`, so grouping needs no binary; a type whose range does not fit its member table is skipped and counted (`ranges_skipped`), never guessed. Signatures, base/nested/interface names and RVAs are still absent — see the skill's Limits.

Two opt-in commands handle protected builds: `recover-metadata` (writes `global-metadata.dat` + `recovery.json`) and `unpack-native` (writes `elf_report.json` + an unpacked `.so` when given `--cmd`).

## `uare package` (packaged/ directory)

RE-oriented tree. Categories: `assets/sprites` (cropped PNGs), `assets/textures` (PNG), `assets/fonts` (TTF/OTF), `assets/audio` (WAV), `assets/videos`, `assets/animations` (AnimationClip JSON), `assets/spines` (`*.atlas` + `*.skel` + `*_SkeletonData.json`), `assets/meshes` (OBJ), `assets/materials` (JSON), `assets/shaders`, `assets/textassets`, `assets/monobehaviours` (JSON). Also `scripts/hotdll/` (hot-update .NET assemblies — the game logic), `scripts/monoscripts.tsv`, `scenes/<levelN>/hierarchy.json`, `prefabs/`, `hierarchies/`, `catalog.json` (one record per object: class, name, category, source, output, note) and `index.html` (filter by class/name/source). Objects that cannot be decoded are listed with a `note` (e.g. `streamed texture`) instead of being dropped.

## `uare export-assets` (assets/ directory)

| File | Content |
|---|---|
| `objects.jsonl` | one record per object: `file`, `path_id`, `class_id`, `class_name`, `name`, `byte_size` |
| `textassets/` | each TextAsset's raw bytes, named after m_Name (CSV/JSON/Lua/config data) |
| `monoscripts.tsv` | `file<TAB>path_id<TAB>name<TAB>class<TAB>namespace<TAB>assembly` per MonoScript |
| `assets_report.json` | per-class object counts, serialized-file and bundle counts |

`name` is filled only for types whose body begins with m_Name (Texture2D, TextAsset, AudioClip, Mesh, Sprite, Shader, Font, MonoScript, ...); other objects have an empty name. Bundles under `assets/aa` (Addressables) are counted but not unpacked.

With `--backend unitypy` (needs `pip install uare[power]`) the same command additionally opens AssetBundles and writes: `textures/*.png`, `audio/*.wav`, `meshes/*.obj`, `monobehaviours/*.json` (typetree dicts; needs a type tree, e.g. Visual Scripting graphs), plus the same `objects.jsonl` and `monoscripts.tsv`. Objects it cannot decode are still counted in `assets_report.json.errors`.

## `uare il2cpp-enrich` (enriched/ directory)

| File | Content |
|---|---|
| `methods_rva.tsv` | `rva<TAB>class<TAB>method<TAB>signature` per method, from Il2CppDumper's `script.json` |
| `dump.cs` | the dumper's generated C# (signatures + structure) |
| `il2cpp.h` | the dumper's generated header, when produced |
| `enrich_report.json` | tool path + source, method count, files |

## `uare decompile` (ghidra/ directory)

| File | Content |
|---|---|
| `functions.tsv` | `address<TAB>name<TAB>size` per function (address matches `methods_rva.tsv` RVAs) |
| `decompiled/*.c` | pseudo-C per function (with `--no-decompile`, the directory is empty) |

# report.json fields (uare analyze)

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
- `version`: metadata version. 24 covers Unity 2017-2018 (24.0-24.4 layouts), 27 covers 2019-2020, 29-30 covers 2021-2022, 31 covers Unity 6. `dump-il2cpp` supports 24, 27, 28, 29, 30, 31.
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
| `packing_verdict` | `likely_packed_or_encrypted` when entropy is at least 7.2, or at least 25% of windows are high entropy, or (encrypted chunk + at least 50% filler windows). `normal` when entropy is at most 7.0. In the 7.0-7.2 band, structure decides: a broken section table or at least 25% filler leans packed, a clean table with symbols and little filler leans normal, otherwise `inconclusive`. `no_exec_segment` when there is no code segment |

A packed library can show low average entropy, because a large filler region dilutes it. The window counts expose that case, which is why the verdict uses them.
