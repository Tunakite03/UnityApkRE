# unity-apk-re

Evidence-backed reverse engineering of Unity Android games for project reconstruction.

The main output is a navigable game blueprint: scenes and hierarchy, confirmed prefab
provenance, direct/transitive asset usage, animation data, script bindings, SDK evidence,
and concrete gaps. Asset exports support that model. `rebuild` creates a structural draft
and reports unresolved bindings; it does not claim a complete playable game.

Python 3.10+ is required. Binary packaging uses the `power` extra, installed into
`.venv-power` inside the plugin (or checkout) root, which `scripts/uare` uses automatically;
see [the tool guide](docs/agent-tools.md#plugin-root-wrapper-and-python-environment).
Blueprint generation uses the standard library. adb is bundled for supported platforms.

## How it works

```text
pull → extract → analyze → package → blueprint
                            │           ↑
                            │     code analysis + runtime evidence
                            ↓
                         rebuild → Unity import / compilation / gameplay validation
```

The plugin guides the agent to investigate behavior with source citations, not infer
rules from class names. Unknown versions, missing references, remote content and
unverified gameplay remain explicit. Authorized work proceeds without a mandatory
repeated scope checkpoint.

See [the reconstruction architecture](docs/redesign.md) for data contracts and limits,
and the [Step-by-Step Guide](docs/guide.md) ([tiếng Việt](docs/huong-dan.md)) for a complete workflow walkthrough.

## Quick start

```bash
git clone <this repo> unity-apk-re && cd unity-apk-re
scripts/uare doctor                                   # which adb, its version, connected devices
scripts/uare packages --filter hero                   # find the package id
scripts/uare run com.example.game --workdir re/game   # includes the reconstruction blueprint
scripts/uare blueprint re/game --out re/game/blueprint-review
scripts/uare rebuild re/game --out re/game/project-draft
```

On Windows, use `scripts\uare.cmd`.

Open `re/game/blueprint/index.html` directly in a browser; it needs no server.
Select a scene, inspect hierarchy/components, follow references and reverse usage,
and review animation objects and missing evidence. `overview.md` is the compact entry
point and `report.json` / `scopes/*.json` are machine-readable.

Existing category-only exports must be repackaged with this version: their path IDs
cannot safely distinguish objects in different serialized files. Use a fresh
output directory within the existing workspace; never overwrite earlier results.

Agents should read [AGENTS.md](AGENTS.md) and the
[workspace and local tool guide](docs/agent-tools.md). The guide explains capture
selection and optional vendored native/managed tools for any supported game.

The default rebuild manifest has no guessed package versions. Explicit reconstruction
choices can be provided in `<workdir>/rebuild-packages.json` under `dependencies`.
Behavior analysis and observations use the contracts in the rebuild skill and
[architecture document](docs/redesign.md).

## As a Claude Code plugin

```text
/plugin marketplace add <github-user>/<this-repo>
/plugin install unity-apk-re@unity-apk-re
```

### Skills (agent guidance)

| Skill | What it guides |
|---|---|
| `/unity-apk-re:rebuild-game` | **Main workflow** -- structural evidence, behavior analysis, reconstruction and verification |
| `/unity-apk-re:analyze-apk` | Extraction and analysis pipeline, from a phone or local files |
| `/unity-apk-re:pull-from-device` | Pulling APK splits and OBBs, fixing device states |
| `/unity-apk-re:capture-device-logs` | Streaming and dumping Unity runtime logs and crash diagnostics via logcat |
| `/unity-apk-re:explore-il2cpp` | Searching IL2CPP names and string literals, reading report.json |
| `/unity-apk-re:operate-and-test` | Dry-run planning, offline tests, verifying existing outputs |

### Reference documents

The `rebuild-game` skill includes reference docs the agent uses during reconstruction:

- `references/unity-project-structure.md` -- exact folder layout for a Unity project
- `references/rebuild-questions.md` -- evidence and input decisions
- `references/scripting-backend.md` -- Mono vs IL2CPP rebuild strategies and SDK detection

## CLI tools

The `uare` CLI handles binary format parsing and tasks that agents cannot do natively. The agent invokes these commands as guided by the skills.

| Command | What it does |
|---|---|
| `doctor` | Shows resolved adb, its version, and connected devices |
| `fetch-adb` | Download official platform-tools adb into `vendor/` |
| `devices` / `packages [--filter WORD]` | List devices / third-party packages on the device |
| `logcat [package]` | Dump, stream and filter device logs (`--dump`, `--follow`, `--grep`, `--unity-only`) |
| `pull <package> --out DIR` | Pull all APK splits and OBBs from a device |
| `extract <archives> --out DIR` | Merge APK splits into one tree |
| `analyze <root> --out DIR` | Report on Unity version, scripting backend, IL2CPP metadata, packing |
| `run <package> --workdir DIR` | Pull + extract + analyze + package + blueprint |
| `plan <package> --workdir DIR` | Dry run: list steps, commands, prerequisites |
| `summary <analysis>` | Print the summary of an existing analysis directory |
| `package <root> --out DIR` | Full categorized asset export (needs the `power` extra in `.venv-power`) |
| `dump-il2cpp <root> --out DIR` | Categorized IL2CPP name tables |
| `find DIR REGEX` | Search identifiers or string literals |
| `blueprint <workdir> --out DIR` | Offline scene/asset browser, per-scope manifests, behavior and dependency evidence |
| `rebuild <workdir> --out DIR` | Create structural draft from game_model.json; retain evidence and blockers |
| `scene-spec <workdir> --out DIR` | One build spec per scene for the `com.uare.scenebuilder` Unity package |
| `assetripper <root> --out DIR` | Native prefabs, AnimationClips, AnimatorControllers and sprite sheets via AssetRipper |
| `assetripper-remap <export> --assets DIR --out DIR` | Match an AssetRipper export to source objects and restore original GUIDs |
| `mono-decompile <managed> --out DIR` | Decompile Mono assemblies via ILSpy |
| `il2cpp-annotate <il2cpp> --out DIR` | Merge IL2CPP names + RVAs + Ghidra into .cs stubs |
| `recover-shaders <source> --out DIR` | Recover ShaderLab source from type-tree JSON |
| `convert-scenes <source> --out DIR` | Convert hierarchy JSON to Unity YAML scenes/prefabs |
| `extract-wwise <source> --out DIR` | Extract Wwise .bnk sound banks to WEM files |
| `generate-meta <root>` | Generate .meta files for all assets |
| `export-assets <root> --out DIR` | Object index + TextAsset + MonoScript table |
| `il2cpp-enrich <root> --out DIR` | Il2CppDumper: RVAs + signatures + dump.cs |
| `decompile <so> --out DIR` | Ghidra headless C decompilation |
| `fetch-tool --url URL --dest DIR` | Download and unpack an external tool archive into `vendor/` (`--sha256` to verify) |
| `recover-metadata <root> --out DIR` | Undo a reversible `global-metadata.dat` wrapper (XOR or `--cmd`) |
| `unpack-native <so> --out DIR` | Identify a native packer and run an external unpacker (`--cmd`) |
| `decrypt-hotdll <dll_dir> --out DIR` | Decrypt HybridCLR-encrypted hot-update DLLs; keys recovered from `global-metadata.dat` and verified, or pass `--keys/--key/--iv` |

Each command writes to a new or empty directory through a staging folder. A failed run leaves nothing behind.

## Scope

The pipeline reads and reports. It does not patch, re-sign or repackage builds. Three
decryption commands are separate steps that `run` does not call; agents run them whenever
protection blocks analysis, without an extra confirmation:

- `recover-metadata` -- undoes a reversible `global-metadata.dat` wrapper
- `unpack-native` -- identifies a native packer and runs a user-supplied unpacker
- `decrypt-hotdll` -- undoes HybridCLR hot-update DLL encryption. The key search only reads
  constants that the C# compiler stored in `global-metadata.dat` under SHA-256-named
  `<PrivateImplementationDetails>` fields (every candidate is verified against its own
  SHA-256 before use), then tries the standard HybridCLR.Security schemes
  (AES-128-CBC, AES-128-GCM, ChaCha20, RC4, XOR) and accepts one only when a whole file
  decrypts to a valid PE. A game with a custom scheme raises instead of guessing; pass
  `--algo/--key/--iv/--magic` or `--keys` to decrypt with your own keys. It never needs
  the device at runtime.

Use it on apps installed on your own device and comply with the app's terms.

## Development

```bash
scripts/run-tests                                     # unit tests (stdlib unittest)
UARE_REAL_DATA_DIR=/path/to/Data scripts/run-tests    # plus a smoke test on a real Unity Data folder
```

Layout:

- `src/uare/` -- Python CLI and binary parsing modules
- `skills/` -- Claude Code skills (agent guidance)
  - `rebuild-game/` -- main evidence-backed reconstruction workflow
  - `analyze-apk/` -- extraction and analysis pipeline
  - `pull-from-device/` -- APK pulling from device
  - `capture-device-logs/` -- runtime logcat capture and filtering
  - `explore-il2cpp/` -- IL2CPP name exploration
  - `operate-and-test/` -- testing and verification
- `.claude-plugin/` -- plugin and marketplace manifests
- `vendor/` -- bundled adb platform-tools, tracked launchers and local optional analysis tools (payloads Git-ignored)
