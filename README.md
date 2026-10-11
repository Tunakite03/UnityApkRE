# unity-apk-re

AI agent guidance plugin for reverse engineering and rebuilding Unity Android games.

This is a Claude Code plugin that **guides** the AI agent through the complete workflow -- from pulling an APK off a phone to producing an openable Unity project. The plugin provides step-by-step instructions, decision checkpoints, and reference documentation. It does not do everything automatically: the agent follows the guidance, uses the bundled CLI tools for binary parsing, and asks the user for decisions at key points.

Requirements: Python 3.10+, standard library only. adb is bundled for macOS, Linux and Windows.

> Vietnamese step-by-step guide: [docs/huong-dan.md](docs/huong-dan.md)

## How it works

When the user says something like **"Rebuild game with package:com.example.game"**, the agent follows this interactive workflow:

```
Phase 1: ACQUIRE       Pull APK from device via adb
Phase 2: EXTRACT & RE  Extract assets, analyze, reverse engineer
Phase 3: CHECKPOINT    Report findings to user, ASK about rebuild scope
Phase 4: REBUILD       Create Unity project based on user's answers
```

The **checkpoint** is the key design choice: after extraction and analysis, the agent reports what it found (Unity version, scripting backend, asset inventory, protections) and asks the user how they want to rebuild (local-only, with backend stubs, or full reconstruction). Only after the user answers does the agent proceed.

## Quick start

```bash
git clone <this repo> unity-apk-re && cd unity-apk-re
scripts/uare doctor                                   # which adb, its version, connected devices
scripts/uare packages --filter hero                   # find the package id
scripts/uare run com.example.game --workdir re/game   # pull + extract + analyze + full asset export
scripts/uare find re/game/analysis '(?i)shop' --context 6
```

On Windows, use `scripts\uare.cmd`.

## As a Claude Code plugin

```text
/plugin marketplace add <github-user>/<this-repo>
/plugin install unity-apk-re@unity-apk-re
```

### Skills (agent guidance)

| Skill | What it guides |
|---|---|
| `/unity-apk-re:rebuild-game` | **Main workflow** -- the full interactive pipeline from APK to Unity project, with user checkpoint |
| `/unity-apk-re:analyze-apk` | Extraction and analysis pipeline, from a phone or local files |
| `/unity-apk-re:pull-from-device` | Pulling APK splits and OBBs, fixing device states |
| `/unity-apk-re:explore-il2cpp` | Searching IL2CPP names and string literals, reading report.json |
| `/unity-apk-re:operate-and-test` | Dry-run planning, offline tests, verifying existing outputs |

### Reference documents

The `rebuild-game` skill includes reference docs the agent uses during each phase:

- `references/unity-project-structure.md` -- exact folder layout for a Unity project
- `references/rebuild-questions.md` -- decision matrix: what to ask and how to interpret answers
- `references/scripting-backend.md` -- Mono vs IL2CPP rebuild strategies and SDK detection

## CLI tools

The `uare` CLI handles binary format parsing and tasks that agents cannot do natively. The agent invokes these commands as guided by the skills.

| Command | What it does |
|---|---|
| `doctor` | Shows resolved adb, its version, and connected devices |
| `pull <package> --out DIR` | Pull all APK splits and OBBs from a device |
| `extract <archives> --out DIR` | Merge APK splits into one tree |
| `analyze <root> --out DIR` | Report on Unity version, scripting backend, IL2CPP metadata, packing |
| `run <package> --workdir DIR` | Pull + extract + analyze + full asset export (one shot) |
| `plan <package> --workdir DIR` | Dry run: list steps, commands, prerequisites |
| `package <root> --out DIR` | Full categorized asset export (needs `uare[power]`) |
| `dump-il2cpp <root> --out DIR` | Categorized IL2CPP name tables |
| `find DIR REGEX` | Search identifiers or string literals |
| `rebuild <workdir> --out DIR` | Auto-create Unity project from workdir |
| `mono-decompile <managed> --out DIR` | Decompile Mono assemblies via ILSpy |
| `il2cpp-annotate <il2cpp> --out DIR` | Merge IL2CPP names + RVAs + Ghidra into .cs stubs |
| `recover-shaders <source> --out DIR` | Recover ShaderLab source from type-tree JSON |
| `convert-scenes <source> --out DIR` | Convert hierarchy JSON to Unity YAML scenes/prefabs |
| `extract-wwise <source> --out DIR` | Extract Wwise .bnk sound banks to WEM files |
| `generate-meta <root>` | Generate .meta files for all assets |
| `export-assets <root> --out DIR` | Object index + TextAsset + MonoScript table |
| `il2cpp-enrich <root> --out DIR` | Il2CppDumper: RVAs + signatures + dump.cs |
| `decompile <so> --out DIR` | Ghidra headless C decompilation |
| `decrypt-hotdll <dll_dir> --out DIR` | Decrypt HybridCLR-encrypted hot-update DLLs; keys recovered from `global-metadata.dat` and verified, or pass `--keys/--key/--iv` |

Each command writes to a new or empty directory through a staging folder. A failed run leaves nothing behind.

## Scope

The default pipeline reads and reports. It does not patch, re-sign or repackage builds. Three **opt-in** commands cross that line only with the user's explicit input:

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
  - `rebuild-game/` -- main interactive rebuild workflow
  - `analyze-apk/` -- extraction and analysis pipeline
  - `pull-from-device/` -- APK pulling from device
  - `explore-il2cpp/` -- IL2CPP name exploration
  - `operate-and-test/` -- testing and verification
- `.claude-plugin/` -- plugin and marketplace manifests
- `vendor/` -- bundled adb platform-tools
