> **Current workflow:** `run` now ends with a reconstruction blueprint. Open
> `blueprint/index.html`; use scene manifests, references, and explicit gaps as the
> primary result. The mandatory checkpoint and category-only rebuild described
> in the older guide below are superseded by [the reconstruction architecture](redesign.md)
> and the current `rebuild-game` skill. Legacy exports require repackaging.
>
> *(A Vietnamese version of this guide is available at [huong-dan.md](huong-dan.md).)*

# Step-by-Step Guide: unity-apk-re

A guide for AI agents and engineers reverse engineering and reconstructing Unity Android games.

This toolkit **guides** an AI agent through an interactive workflow: pulling APK splits from an authorized device, extracting and analyzing contents, reporting findings, and generating a Unity project draft or navigable reconstruction blueprint. The toolkit does not act as an opaque black box—it defines explicit pipelines while the agent uses bundled CLI tools to parse binary formats and analyze evidence.

The main CLI launcher is `scripts/uare` (Windows: `scripts\uare.cmd`). It requires Python 3.10+; the core pipeline uses only the Python standard library. Platform-specific `adb` binaries are bundled in `vendor/platform-tools/`.

To open AssetBundles and decode textures, audio clips, and meshes (the "power" mode), install the optional dependencies: `pip install "uare[power]"` (UnityPy + Pillow). External binary tools (Il2CppDumper, Ghidra) are optional and not bundled; specify them with `--dumper`/`--ghidra` or place them in `vendor/` using `fetch-tool`.

## Interactive Rebuild Workflow

When requested to **"Rebuild game with package:com.example.game"**, the pipeline proceeds through:

1. **ACQUIRE** — Pull APK splits and OBB expansion files from the device via `adb`.
2. **EXTRACT & RE** — Unpack archives, merge split assets, and perform structural analysis.
3. **CHECKPOINT / BLUEPRINT** — Report Unity version, scripting backend, asset inventory, and packer status. Establish rebuild scope and focus areas (UI, logic, assets).
4. **REBUILD** — Generate a reconstruction blueprint and Unity project draft with explicit blockers.

For detailed agent instructions, see skill `/unity-apk-re:rebuild-game`.

---

## Step 0: Device Preparation

1. Open **Settings → About phone** and tap "Build number" 7 times to enable **Developer options**.
2. Under Developer options, enable **USB debugging**. On Xiaomi / MIUI devices, also enable "USB debugging (Security settings)" if present.
3. Connect the phone to your workstation using a data-capable USB cable.
4. When prompted with "Allow USB debugging?", confirm the RSA key fingerprint and tap **Allow**.

## Step 1: Environment Diagnostics (`doctor`)

```bash
scripts/uare doctor
```

This diagnostic command verifies:

- **Active ADB binary resolution**. Priority order:
  1. `--adb <path>` CLI option
  2. `UARE_ADB` environment variable
  3. Bundled ADB under `vendor/platform-tools/<os>/`
  4. `adb` found in system `PATH`
- **ADB version**
- **Connected device status**. Devices must report `device` state to proceed.

| Status | Resolution |
| --- | --- |
| No devices listed | Check USB cable, reconnect, ensure USB debugging is enabled |
| `unauthorized` | Unlock the phone screen and accept the RSA host key dialog |
| `offline` | Reconnect the USB cable or restart ADB (`adb kill-server`) |

## Step 2: Package Identification (`packages`)

```bash
scripts/uare packages --filter hero
```

Lists non-system user-installed packages matching the filter substring (for example, `com.percent.aos.rollinghero`).

## Step 3: APK Acquisition (`pull`)

```bash
scripts/uare pull com.percent.aos.rollinghero --out re/rollinghero/pull
```

This command executes three operations:

1. Queries package files via `pm path`. Modern Google Play titles distribute split APKs:
   - `base.apk`: Core manifest, bytecode, and primary engine assets.
   - `split_config.arm64_v8a.apk`: Native CPU libraries (`libil2cpp.so`, `libunity.so`).
   - `split_*.apk`: Additional asset packs, language packs, or density resources.
2. Discovers OBB expansion files located under `/sdcard/Android/obb/<package>/`.
3. Downloads all components via `adb pull`, computes SHA-256 digests, and generates `manifest.json` recording package name, device serial, version code, file sizes, and checksums.

Output: `pull/apks/*.apk`, `pull/obb/*.obb`, `pull/manifest.json`.

## Step 4: Archive Extraction & Assembly (`extract`)

```bash
scripts/uare extract re/rollinghero/pull --out re/rollinghero/extracted
```

APKs and OBBs are standard ZIP containers. `extract` merges splits into a unified directory structure:

| Directory | Contents |
| --- | --- |
| `Data/` | Unity engine data (`assets/bin/Data`): `level0..N`, `sharedassets*.assets`, `globalgamemanagers`, IL2CPP metadata |
| `lib/<abi>/` | Native binaries: `libil2cpp.so` (compiled C# bytecode), `libunity.so` (engine core) |
| `assets/` | External runtime assets: Addressables (`assets/aa/`), `StreamingAssets/` |
| `archives/<apk_name>/` | APK-specific leftovers: `AndroidManifest.xml`, `classes.dex` (Dalvik/ART code), `res/` |

Key safety and integrity guarantees:

- **Split `.splitN` file reassembly**: Unity splits oversized asset files into segments (`level0.split0`, `level0.split1`). The tool concatenates them in strict index order and fails if any segment is missing.
- **Path traversal & zip bomb protection**: Rejects entries with directory traversal (`..`), absolute paths, Windows drive letters, case-collision anomalies, or sizes exceeding 64 GiB / available disk space.
- **Conflict detection**: Conflicting files across splits with differing contents raise immediate errors; identical duplicates are deduplicated safely.

## Step 5: Static Inspection (`analyze`)

```bash
scripts/uare analyze re/rollinghero/extracted --out re/rollinghero/analysis
```

Performs header parsing across all components and generates `report.json` along with detailed inventories:

1. **`Data/` inventory (`files.jsonl`)**: Inspects file headers to extract:
   - File format: SerializedFile (scenes, assets), `.resS`/`.resource` streams (textures, audio), AssetBundles (`UnityFS`).
   - Unity engine version, target platform (`Android = 13`), presence of type trees, and object type count.
   - AssetBundle compression algorithms (LZ4, LZMA).
2. **`assets/` Addressables inventory**: Cataloged into `assets_files.jsonl`.
3. **Scripting backend identification**:
   - `il2cpp`: C# compiled to native machine code in `libil2cpp.so`; type and method metadata stored in `Data/Managed/Metadata/global-metadata.dat`.
   - `mono`: .NET managed assemblies located under `Data/Managed/`, decompiable directly with standard .NET decompilers.
4. **IL2CPP metadata parsing (`Data/Managed/Metadata/global-metadata.dat`)**:
   - Validates magic header `0xFAB11BAF`. Non-matching magic indicates encryption or obfuscation, failing loudly.
   - `il2cpp/identifiers.tsv`: Complete list of class, method, field, and namespace names.
   - `il2cpp/string_literals.jsonl`: All embedded C# string constants (UI text, configuration keys, API endpoints).
5. **Native library analysis (`lib/*/*.so`)**:
   - Parses ELF headers: target CPU architecture, code segments, and exported `il2cpp_*` symbols.
   - **Sliding-window Shannon entropy**: Measures byte entropy across consecutive 1 MB windows. Normal compiled code registers between 5.7 and 7.0 bits/byte. Compressed/encrypted payloads approach 8.0 bits/byte. Zero/padding blocks register near 0.0–1.0.
   - Output verdicts: `normal`, `likely_packed_or_encrypted`, or `inconclusive`. Marginal ranges (7.0–7.2) leverage structural ELF signals (corrupted section tables vs valid symbol tables).

*Windowed analysis rationale:* In titles like *Rolling Hero*, `libil2cpp.so` features an encrypted 32 MB prefix followed by repetitive `AGAG` padding bytes. Average whole-file entropy was 3.82 (falsely appearing uncompressed), whereas windowed counts (31 high-entropy windows, 78 padding windows) correctly exposed packing.

To execute Steps 3 through 5 in a single unified pass:

```bash
scripts/uare run com.percent.aos.rollinghero --workdir re/rollinghero
```

## Step 6: Querying Analysis Results (`summary` & `find`)

```bash
scripts/uare summary re/rollinghero/analysis
scripts/uare find re/rollinghero/analysis '(?i)shop|product' --context 8
scripts/uare find re/rollinghero/analysis '(?i)buy|price' --literals --context 2
```

- `summary`: Prints the structured terminal summary of the analysis report.
- `find`: Performs regex searches across extracted symbol tables and strings:
  - `>` marks matching lines.
  - `--context N` displays surrounding lines. Because metadata definitions are stored in declaration order, context lines reveal neighboring fields and methods belonging to the same type.
  - `--literals` searches string literals instead of identifiers.
  - Exit codes: `0` = matches found, `1` = no match, `2` = error.

For field definitions in `report.json`, see [skills/explore-il2cpp/references/report-fields.md](../skills/explore-il2cpp/references/report-fields.md).

## Step 7: Exporting Names & Assets (`dump-il2cpp` & `export-assets`)

```bash
scripts/uare dump-il2cpp re/rollinghero/extracted --out re/rollinghero/il2cpp
scripts/uare export-assets re/rollinghero/extracted --out re/rollinghero/assets
```

`dump-il2cpp` categorizes names from `global-metadata.dat` into structured TSV tables:

| File | Content |
| --- | --- |
| `types.tsv` | `index <TAB> namespace <TAB> type_name` for all declared classes and structs |
| `methods.tsv`, `fields.tsv`, `parameters.tsv`, `properties.tsv`, `events.tsv` | `index <TAB> identifier_name` for each member category |
| `members.tsv` | `type_index <TAB> type_name <TAB> member_kind <TAB> member_index <TAB> member_name` — **members grouped by declaring class** |
| `sections.json` | Metadata version, layout format, section offsets and sizes |

`members.tsv` groups members by type definition ranges (`methodStart`, `method_count`, `fieldStart`, `field_count`) directly from metadata structures. **This grouping does not require unpacking the native binary or executing external dumpers.** Types with inconsistent range tables are skipped and recorded in `ranges_skipped`. Full type signatures (parameters, return types), inheritance hierarchies, and method runtime virtual addresses (RVAs) require `Il2CppType[]` arrays inside `libil2cpp.so` via Il2CppDumper.

`export-assets` parses SerializedFiles and outputs:

| File | Content |
| --- | --- |
| `objects.jsonl` | One record per object: `file`, `pathID`, `class`, `name`, `size` — full asset inventory |
| `textassets/` | Raw bytes of each TextAsset, named by `m_Name` — game configuration tables (CSV, JSON, Lua) |
| `monoscripts.tsv` | `file, pathID, name, class, namespace, assembly` — maps assets to IL2CPP classes |
| `assets_report.json` | Aggregated object counts per class |

## Step 8 (Opt-in Boundary): Protection Removal (`recover-metadata` & `unpack-native`)

By default, the pipeline only inspects and reports. It does not tamper with encrypted or packed targets. The following commands require deliberate manual invocation:

```bash
# Metadata with corrupted/encrypted magic: attempt automated heuristic recovery
scripts/uare recover-metadata re/rollinghero/extracted --out re/rollinghero/meta

# Or supply game-specific keys / decryption commands
scripts/uare recover-metadata re/rollinghero/extracted --out re/rollinghero/meta --key 5a5a5a5a
scripts/uare recover-metadata re/rollinghero/extracted --out re/rollinghero/meta --cmd 'python3 my_decrypt.py {input} {output}'

# Dump IL2CPP names directly from recovered metadata
scripts/uare dump-il2cpp re/rollinghero/extracted --out re/rollinghero/il2cpp --decrypt

# libil2cpp.so is packed: identify known packer signature, then run external unpacker
scripts/uare unpack-native re/rollinghero/extracted/lib/arm64-v8a/libil2cpp.so --out re/rollinghero/unpacked
scripts/uare unpack-native re/rollinghero/extracted/lib/arm64-v8a/libil2cpp.so --out re/rollinghero/unpacked --cmd 'frida-dump {input} {output}'
```

- `recover-metadata`: Tests 1-byte XOR, short-key XOR (1–4 bytes derived from expected magic), user keys via `--key`, or external transformation commands via `--cmd`. Only payloads yielding valid `0xFAB11BAF` magic are accepted.
- `unpack-native`: Detects known mobile packers (SecNeo, Bangcle, 360 Jiagu, Tencent Legu, DingDong, Baidu). Real memory dumping requires `--cmd` (e.g., Frida or custom dumper). The tool validates the unpacked result by remeasuring code entropy.
- Command templates use `{input}` and `{output}` placeholders and execute directly without shell string interpolation.

## Step 9: Clean Binary Analysis (Enrichment, Decompilation, UnityPy)

For uncompressed/clean builds (`verdict: normal`), execute deep symbol and code enrichment:

```bash
# RVAs, signatures, and dump.cs via Il2CppDumper (requires local tool installation)
scripts/uare il2cpp-enrich re/rollinghero/extracted --out re/rollinghero/enriched \
    --dumper /path/to/Il2CppDumper

# Or install tool into vendor/ directory for auto-discovery
scripts/uare fetch-tool --url https://github.com/Perfare/Il2CppDumper/releases/download/vX/Il2CppDumper.zip \
    --dest vendor/il2cpp-dumper
scripts/uare il2cpp-enrich re/rollinghero/extracted --out re/rollinghero/enriched

# Ghidra headless: function disassembly and decompiled pseudo-C matched to RVAs
scripts/uare decompile re/rollinghero/unpacked/libil2cpp.so --out re/rollinghero/ghidra \
    --ghidra /path/to/ghidra/support/analyzeHeadless

# Full asset export with UnityPy (requires uare[power])
pip install "uare[power]"
scripts/uare export-assets re/rollinghero/extracted --out re/rollinghero/assets --backend unitypy

# Standardized reconstruction asset package
scripts/uare package re/rollinghero/extracted --out re/rollinghero/packaged
```

Output deliverables:

| Command | Output Files |
| --- | --- |
| `il2cpp-enrich` | `methods_rva.tsv` (`rva, class, method, signature`), `dump.cs`, `il2cpp.h`, `enrich_report.json` |
| `decompile` | `functions.tsv` (`address, name, size`), `decompiled/*.c` |
| `export-assets --backend unitypy` | `objects.jsonl`, `textassets/`, `textures/` (PNG), `audio/` (WAV), `meshes/` (OBJ), `monobehaviours/` (JSON), `monoscripts.tsv` |

- `il2cpp-enrich` invokes Il2CppDumper with CLI syntax `Il2CppDumper <libil2cpp.so> <global-metadata.dat> <out_dir>`. Tool discovery order: `--dumper`, `UARE_IL2CPP_DUMPER`, `vendor/il2cpp-dumper/`, PATH.
- `decompile` launches `analyzeHeadless` using a dedicated Java post-analysis script (eliminating Jython dependencies). The wrapper script under `vendor/ghidra/analyzeHeadless` configures the bundled JDK 21. See [docs/agent-tools.md](agent-tools.md) for local environment details.
- Function addresses in `functions.tsv` match RVAs in `methods_rva.tsv`, allowing seamless correlation between C# declarations and Ghidra decompilation.

## Step 10: Complete Asset Packaging (`package`)

The unified `run` command executes `package` following analysis, writing into `<workdir>/extracted-assets/`. Requires `pip install "uare[power]"`.

To run asset packaging independently:

```bash
scripts/uare package <workdir>/extracted --out <workdir>/extracted-assets
```

Directory structure of `extracted-assets/`:

```
extracted-assets/
  shared/<category>/<group>/     one canonical copy of every exported file: sprites, textures, fonts,
                                 audio, animations, animators, spines, meshes, materials, shaders,
                                 textassets, monobehaviours, videos, sprite_atlases
  scenes/<SceneName>/            one folder per scene
    scene.json                   GameObject hierarchy of the scene (scene name = its container path;
                                 Data scenes keep their level name, for example level0)
    manifest.json                assets the scene needs (static reference closure), references and gaps
    <category>/<group>/...       hard links into shared/: the same bytes, no extra storage
  prefabs/<group>/<PrefabName>/  one folder per prefab, holding only that prefab's root tree
    prefab.json, manifest.json   and hard links as for scenes
  spines/<group>/<SpineName>/    skeleton, atlas and page images (hard links), the SkeletonDataAsset
                                 dump and manifest.json (pages linked, unresolved or ambiguous)
  evidence/
    bundles/<group>/             AssetBundle and PreloadData indexes (container lists)
    unclassified/objects/        type-tree JSON for objects without a dedicated exporter
    unclassified/hierarchies/    GameObject trees that are neither a scene nor a prefab
  scripts/
    hotdll/                      hot-update assemblies, still encrypted (see decrypt-hotdll below)
    monoscripts.tsv              MonoScript table: class, namespace, assembly
  catalog.json                   every object: class, name, category, source, output path, or note
  index.html                     self-contained offline browser for catalog.json
  report.json                    object counts, scene/prefab/spine folders, hard links, per-file errors
  game_model.json                scoped identities, fields and resolved/missing references
```

Scene and prefab folders contain hard links, not copies: editing a file inside a scene folder edits the
shared copy. `zip` and `rsync` without `-H` store each link as a separate copy.

`<group>` is derived from bundle identifiers (e.g., YooAsset bundle names stripped of `gamepackage_` prefixes and hash tags, such as `assets_gameassets_sprite_pet/`). Objects from the same bundle reside in the same group directory under `shared/`.

- **YooAsset bundle support**: Some builds prepend custom header bytes before the `UnityFS` magic identifier. `package` automatically detects and skips this prefix (`bundle_offset`) without needing decryption.
- **Hot-update DLL detection**: Bundled assemblies (e.g., `GameHotfix_*.dll`) missing standard PE headers are preserved under `scripts/hotdll/` for subsequent decryption.
- Unexportable objects (0×0 placeholder textures, malformed type trees) are logged into `catalog.json` with explicit explanations.

## Step 11 (Opt-in Boundary): Hot-Update DLL Decryption (`decrypt-hotdll`)

Games utilizing **HybridCLR** or similar runtimes encrypt hot-update assemblies as `MAGIC || ciphertext` and embed decryption keys inside the Ahead-of-Time (AOT) metadata: the C# compiler places constant byte arrays (`new byte[]{...}`) into the `<PrivateImplementationDetails>` class, **where field names are the SHA-256 hashes of the byte arrays**, and raw values reside in the `fieldAndParameterDefaultValueData` blob of `global-metadata.dat`.

```bash
scripts/uare decrypt-hotdll "<workdir>/extracted-assets/scripts/hotdll" --out "<workdir>/hotdll-decrypted"
```

- Decryption mechanics:
  1. Extracts all candidate constant byte arrays from metadata.
  2. **Validates each candidate array against its declaring field name via SHA-256** (eliminating false guesses).
  3. Tests standard cipher schemes: AES-128-CBC, AES-128-GCM, ChaCha20, RC4, and XOR across standard magic prefixes.
  4. Accepts a candidate key only when the decrypted output yields a **valid PE/COFF header** and valid PKCS#7 padding.
- Does not require `libil2cpp.so`—keys are recovered entirely from metadata constants.
- Pure Python fallback included; install `pip install "uare[crypto]"` for PyCryptodome acceleration.
- Generates `decrypted/*.dll` (decompilable via ILSpy), `keys.json` (reusable with `--keys`), and `report.json`.
- Custom algorithms can be specified explicitly: `--algo <cipher> --key <hex> --iv <hex> --magic <hex>`.

## Step 12: Rebuild Scenes One at a Time in Your Own Unity Project (`scene-spec` + scene builder)

Create the Unity project yourself (matching Unity version, render pipeline of your choice) and install Unity MCP. The agent then rebuilds one scene at a time into that project.

```bash
scripts/uare scene-spec "<workdir>" --assets extracted-assets --out "<workdir>/scene-specs"
```

- Writes one `<Scene>.json` per scene folder: GameObjects in hierarchy order, every serialized field flattened to a Unity property path, each reference resolved to an object of the scene, an exported file under `shared/`, or an explicit `missing`/`unresolved` entry, plus the scene's assets (sprite pivot/PPU/border, material shader/texture/color) and the original tags, layers and sorting layers.
- Add the builder package to the project's `Packages/manifest.json`: `"com.uare.scenebuilder": "file:<path to this repo>/unity/com.uare.scenebuilder"`. Keep its `.meta` files: placeholders in built scenes reference the placeholder script GUID.
- Build one scene through MCP `execute_code`: `return Uare.SceneBuilder.Editor.UareSceneBuilder.BuildScene("<spec>.json", "Assets/<Game>");`. The scene is saved to `Assets/<Game>/Scenes/`, its files to `Assets/<Game>/Shared/`. The call returns a JSON report (properties applied/unmatched, references bound/missing, placeholder scripts, shader substitutions, settings changes).
- Safety: everything is validated before the first write; an existing scene or a differing project file is never replaced; a failed build leaves an empty scene, not a half-built one. To rebuild a scene, delete its `.unity` file first.
- Scripts that are not in the project become `UareMissingScript` placeholders that store the original class, namespace, assembly, object ID and fields. Shaders the project lacks are replaced by URP sprite or UI shaders and listed in the report.
- Tests: add `"testables": ["com.uare.scenebuilder"]` to the manifest. EditMode tests cover property writing; the PlayMode test loads every scene under `Assets` for 120 frames and appends results to `Logs/uare-playmode.jsonl`.
- Spec v2 also carries each scene's RenderSettings and LightmapSettings, every saved material property (textures with scale/offset, floats, ints, colors, keywords, render queue), the original texture settings (filter, wrap, mipmaps, readable, Alpha8) and ScriptableObject assets such as TMP font assets and Spine `SkeletonDataAsset`/`SpineAtlasAsset`, rebuilt from their own fields when their script exists in the project. Text files keep their original TextAsset name; `.skel` becomes `.skel.txt` and `.atlas` becomes `.atlas.bytes`, because spine-unity's importer acts on `.skel.bytes`/`.atlas.txt` and can open modal dialogs.
- Root order: a build keeps no editor root order. A probe bundle on 2022.3 showed that Unity loads the roots of a built scene in path-ID order, so the rebuilt order (`rootOrderBasis: path_id`) is the order the shipped game sees at runtime.
- Game code (only with the user's explicit authorization to decrypt): decrypt with `decrypt-hotdll`, run `il2cpp-enrich` for Il2CppDumper stub DLLs of the AOT assemblies, copy the decrypted hot-update DLLs plus the stubs they reference into the project, and turn off Auto Reference for all of them (`isExplicitlyReferenced: 1`), or a game type such as a global `ObjectPool<T>` can break package compilation. Stub DLLs need two repairs before the Editor loads them cleanly: attribute type arguments stored without an assembly name, and the bodiless `UnitySourceGeneratedAssemblyMonoScriptTypes_v1` type. The builder then binds scripts to the real hot-update types, reports stub-bound scripts separately (`stubBoundScripts`: original fields, no original logic), and keeps placeholders only for types that are absent.
- Spine 3.8 data needs spine-unity 3.8. Its git package lacks spine-csharp, so install it as an embedded package: `spine-unity/Assets/Spine` plus `spine-csharp/src` copied into `Runtime/spine-csharp`.

---

## Technical Limitations

1. **Member grouping vs type signatures**: `members.tsv` groups members by declaring type from metadata alone. However, parameter types, return types, inheritance chains, and method entry addresses (RVAs) require `Il2CppType[]` tables from an unpacked `libil2cpp.so`.
2. **Metadata versions**: Validated layouts support versions 24 (24.2, 24.4 distinguished by header size), 27, 28, 29, 30, and 31. Older versions fall back to flat identifier extraction via `analyze`.
3. **SerializedFile formats**: Supports format versions ≥ 17 (Unity 2017.1+). Earlier formats are recorded in header inspection but rejected during deep object extraction.
4. **Obfuscated identifiers**: Stripped or obfuscated symbols are tracked and left blank; the tool never invents or guesses names.
5. **Media codecs**: Standard `export-assets` extracts raw TextAsset bytes and catalogs objects, but does not decompress complex textures or audio codecs (ETC, ASTC, Vorbis). Use `package` with `uare[power]` (UnityPy) for decoded media.
6. **Encrypted boundaries**: Custom proprietary packing or encryption requires user-supplied keys or external unpackers via `--key`, `--keys`, or `--cmd`.

---

## Safety Guarantees

- **No in-place overwrites**: Commands write to distinct new or empty directories via temporary staging locations. Interrupted executions leave no corrupted states behind.
- **Loud failure**: Errors log structured JSON payloads detailing failure reason and exit code 2. Exceptions are never silently swallowed.
- **ADB command sanitization**: Package identifiers and paths retrieved from mobile devices are validated against strict regular expressions before command assembly.
- **Verifiable vendor binaries**: Bundled ADB archives originate directly from official Google repositories, tracked by `FETCHED.json` containing source URLs, revisions, and SHA-256 digests.

---

## Claude Code Plugin Integration

To install as a Claude Code plugin:

```text
/plugin marketplace add <github-user>/<repo>
/plugin install unity-apk-re@unity-apk-re
```

Available skills:

| Skill | Use Case |
| --- | --- |
| `/unity-apk-re:rebuild-game` | **Main workflow** — structural evidence, behavior analysis, and project reconstruction |
| `/unity-apk-re:analyze-apk` | Execute the extraction and analysis pipeline; interpret outputs |
| `/unity-apk-re:pull-from-device` | Acquire APK splits from connected hardware; troubleshoot device states |
| `/unity-apk-re:capture-device-logs` | Stream and dump Unity engine and crash diagnostics via logcat |
| `/unity-apk-re:explore-il2cpp` | Query IL2CPP identifiers, analyze string constants, and parse report fields |
| `/unity-apk-re:operate-and-test` | Dry-run pipelines, verify existing workspaces, and run offline test suites |
