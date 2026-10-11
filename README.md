# unity-apk-re

Static recon toolkit for Unity Android games, packaged as a Claude Code plugin and a standalone CLI (`uare`).

It pulls an installed game's APK splits and OBBs from a USB-connected phone using a bundled adb. It merges them into one tree, then reports on:

- the Unity data
- the scripting backend
- IL2CPP metadata (every class, method and field name, plus every C# string literal)
- whether the native code is packed

Requirements: Python 3.10+, standard library only. adb is bundled for macOS, Linux and Windows.

> Vietnamese step-by-step guide: [docs/huong-dan.md](docs/huong-dan.md)

## Quick start

```bash
git clone <this repo> unity-apk-re && cd unity-apk-re
scripts/uare doctor                                   # which adb, its version, connected devices
scripts/uare packages --filter hero                   # find the package id
scripts/uare run com.example.game --workdir re/game   # pull + extract + analyze
scripts/uare find re/game/analysis '(?i)shop' --context 6
```

On Windows, use `scripts\uare.cmd`.

## As a Claude Code plugin

```text
/plugin marketplace add <github-user>/<this-repo>
/plugin install unity-apk-re@unity-apk-re
```

Skills:

| Skill | Use it for |
|---|---|
| `/unity-apk-re:analyze-apk` | The whole pipeline, from a phone or from local .apk/.obb files, and how to read the result |
| `/unity-apk-re:pull-from-device` | Pulling splits and OBBs, and fixing device states (unauthorized, offline, ...) |
| `/unity-apk-re:explore-il2cpp` | Searching names and string literals, and explaining `report.json` |

## Commands

| Command | What it does | Output |
|---|---|---|
| `doctor` | Shows the resolved adb (`--adb`, `UARE_ADB`, vendored, PATH), its version, and devices | stdout |
| `fetch-adb [--platform darwin\|linux\|windows\|all]` | Re-downloads official platform-tools into `vendor/`, records sha256 + revision | `vendor/platform-tools/<os>/` |
| `devices`, `packages [--filter]` | Lists devices and third-party packages | stdout |
| `pull <package> --out DIR` | `pm path` + OBB listing + `adb pull` of every file | `apks/`, `obb/`, `manifest.json` |
| `extract <apks/obbs or pull dir> --out DIR` | Merges splits, concatenates `.splitN`, rejects unsafe entries | `Data/`, `lib/<abi>/`, `assets/`, `archives/<apk>/`, `extraction.json` |
| `analyze <extracted dir or Unity Data dir> --out DIR` | Inventory, backend, IL2CPP tables, ELF report | `report.json`, `files.jsonl`, `assets_files.jsonl`, `il2cpp/` |
| `run <package> --workdir DIR` | `pull`, then `extract`, then `analyze` | `pull/`, `extracted/`, `analysis/` |
| `summary DIR` | Re-prints the summary of an analysis | stdout |
| `find DIR REGEX [--literals] [--context N]` | Searches identifiers or string literals | stdout (exit 1 = no match) |
| `dump-il2cpp <root> --out DIR` | Categorised IL2CPP name tables + member grouping from global-metadata.dat | `types.tsv`, `methods.tsv`, `fields.tsv`, `parameters.tsv`, `properties.tsv`, `events.tsv`, `members.tsv`, `sections.json` |
| `export-assets <root> --out DIR [--backend stdlib\|unitypy\|auto]` | Object index + TextAsset bytes + MonoScript table; `unitypy` also opens AssetBundles and decodes media | `objects.jsonl`, `textassets/`, `monoscripts.tsv`, `assets_report.json` (`unitypy` adds textures/audio/meshes/monobehaviours) |
| `package <root> --out DIR` | RE-oriented tree: categorised assets + hierarchies + index (needs `uare[power]`) | `assets/{sprites,textures,fonts,audio,videos,animations,spines,meshes,materials,shaders,textassets,monobehaviours}/`, `scripts/hotdll/`, `scenes/`, `prefabs/`, `hierarchies/`, `catalog.json`, `index.html` |
| `recover-metadata <root> --out DIR [--key HEX] [--cmd CMD]` | Recover a wrapped `global-metadata.dat` (XOR or external decryptor), opt-in | `global-metadata.dat`, `recovery.json` |
| `unpack-native <so> --out DIR [--cmd CMD]` | Identify a native packer; optionally run an external unpacker | `elf_report.json`, the unpacked `.so` |
| `il2cpp-enrich <root> --out DIR [--dumper PATH]` | Run Il2CppDumper/Inspector: RVAs, signatures, `dump.cs` | `methods_rva.tsv`, `dump.cs`, `il2cpp.h`, `enrich_report.json` |
| `decompile <so> --out DIR [--ghidra PATH]` | Ghidra headless: function list + C decompilation | `functions.tsv`, `decompiled/*.c` |
| `fetch-tool --url URL --dest DIR [--sha256]` | Download + unpack an external tool into `vendor/` | the tool tree, `FETCHED.json` |

`export-assets` also takes `--backend stdlib|unitypy|auto`: with `UnityPy` installed (`pip install uare[power]`) it opens AssetBundles and decodes Texture2D/Sprite (PNG), AudioClip (WAV), Mesh (OBJ) and MonoBehaviour typetrees (JSON, incl. Visual Scripting graphs).

Each command writes to a new or empty directory through a staging folder. A failed run leaves nothing behind, and earlier results are never overwritten. Errors exit with code 2 and a structured log line.

## Bundled adb

`vendor/platform-tools/<os>/` holds `adb`, `NOTICE.txt`, `source.properties` and `FETCHED.json`. On Windows it also holds `AdbWinApi.dll` and `AdbWinUsbApi.dll`.

- `FETCHED.json` records the source URL, revision and sha256 values.
- These files are unmodified copies from Google's Android SDK Platform-Tools.
- The SDK and its components are subject to the [Android Software Development Kit License Agreement](https://developer.android.com/studio/terms). The open-source notices are in each `NOTICE.txt`.
- To update or verify them, run `scripts/uare fetch-adb --platform all` after deleting `vendor/platform-tools/`.

## Scope

The default pipeline reads documented formats and reports. It does not patch, re-sign or repackage builds, and by default it does not bypass packers or encrypted metadata: it detects them and says so.

Two **opt-in** commands cross that boundary on purpose, only with the user's explicit input:

- `recover-metadata` undoes a reversible `global-metadata.dat` wrapper (single/short repeating XOR derived from the magic, a caller-supplied `--key`, or a caller-supplied `--cmd` decryptor). It never guesses: the result must pass the real header check.
- `unpack-native` names a recognised native packer from in-file markers and, given a caller-supplied `--cmd` unpacker (for example a Frida memory-dump script), runs it and re-checks the result with the same entropy/structure analysis.

Neither is automatic; nothing is written unless you invoke them.

For **unpacked** builds, four more commands extend the report without crossing any protection boundary: `il2cpp-enrich` (calls Il2CppDumper/Inspector for RVAs, signatures and `dump.cs`), `decompile` (Ghidra headless function list + C), the `--backend unitypy` mode of `export-assets`, and `package` (a categorised RE tree). External binaries are located via flags/env/`vendor/`/PATH and fetched with `fetch-tool`; the Python extra is `pip install uare[power]`.

`package` opens plain `UnityFS` bundles **and** bundles whose `UnityFS` magic sits behind a short header prefix (common with YooAsset), by detecting and skipping the prefix — it does not decrypt. It sorts objects the way a reverse engineer reads them: `assets/sprites`, `textures`, `fonts` (TTF/OTF), `audio`, `videos`, `animations`, `spines` (atlas + skel + SkeletonData), `meshes` (OBJ), `materials`, `shaders`, `textassets`, `monobehaviours`; plus `scripts/hotdll/` (hot-update .NET assemblies = the game logic, decompile with ILSpy/dnSpy), `scenes|prefabs|hierarchies/` (GameObject trees), and a `catalog.json` + filterable `index.html`.

Concretely, for IL2CPP, `dump-il2cpp` reads the `global-metadata.dat` definition tables and emits the type/method/field/property/event **names** that the format exposes, and groups methods/fields/properties/events under their declaring type (`members.tsv`) — the ranges for that come straight from the metadata, no binary needed. It does **not** resolve signatures, base/nested/interface names or code addresses: those need the `Il2CppType[]` array that lives in `libil2cpp.so`, so they are the job of Il2CppDumper/Il2CppInspector on an unpacked binary. For assets it extracts TextAsset data and indexes every object, but does not decode compressed textures/audio/meshes (use AssetRipper/UnityPy). Obfuscated name entries are reported and left empty, never guessed.

Formats: `dump-il2cpp` selects a per-version layout for metadata v24, 27, 28, 29, 30 and 31 (a wrong guess fails validation rather than emitting garbage); object export handles SerializedFile format 17+ (Unity 2017.1+), with the header and object table adapted below format 22; the packing verdict combines entropy with structure (section table, filler fraction) to narrow the `inconclusive` band without guessing.

Use it on apps installed on your own device and comply with the app's terms.

## Development

```bash
scripts/run-tests                                     # unit tests (stdlib unittest)
UARE_REAL_DATA_DIR=/path/to/Data scripts/run-tests    # plus a smoke test on a real Unity Data folder
```

Layout:

- `src/uare/`
  - `adb.py`: device adapter
  - `apk.py`: APK-set merge
  - `unity.py`: SerializedFile and AssetBundle headers
  - `serialized.py`: SerializedFile type/object tables
  - `assets.py`: object index, TextAsset/MonoScript export
  - `il2cpp.py`: metadata tables
  - `il2cpp_dump.py`: categorised names + member grouping
  - `elf.py`: segments, symbols, entropy and packer markers
  - `packers.py`: known native-packer signatures
  - `recover.py` / `external.py`: opt-in metadata recovery and external-tool hooks
  - `unitypy_backend.py`: UnityPy asset export (power extra)
  - `package_tree.py`: RE-oriented categorised tree + hierarchy + catalog (power extra)
  - `il2cpp_tools.py`: Il2CppDumper/Inspector adapter
  - `ghidra.py`: Ghidra headless decompilation
  - `tool_fetch.py`: download/unpack an external tool into vendor/
  - `analyze.py`: report
  - `platform_tools.py`: vendored adb
  - `search.py`: `find`
  - `cli.py`: command line
- `skills/`: Claude Code skills
- `.claude-plugin/`: plugin and marketplace manifests
