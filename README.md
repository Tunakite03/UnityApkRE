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

Each command writes to a new or empty directory through a staging folder. A failed run leaves nothing behind, and earlier results are never overwritten. Errors exit with code 2 and a structured log line.

## Bundled adb

`vendor/platform-tools/<os>/` holds `adb`, `NOTICE.txt`, `source.properties` and `FETCHED.json`. On Windows it also holds `AdbWinApi.dll` and `AdbWinUsbApi.dll`.

- `FETCHED.json` records the source URL, revision and sha256 values.
- These files are unmodified copies from Google's Android SDK Platform-Tools.
- The SDK and its components are subject to the [Android Software Development Kit License Agreement](https://developer.android.com/studio/terms). The open-source notices are in each `NOTICE.txt`.
- To update or verify them, run `scripts/uare fetch-adb --platform all` after deleting `vendor/platform-tools/`.

## Scope

The toolkit reads and reports. It does not unpack, decrypt, patch, re-sign or repackage builds. It does not bypass packers, encrypted metadata or other protections; it detects them and says so.

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
  - `il2cpp.py`: metadata tables
  - `elf.py`: segments, symbols and entropy
  - `analyze.py`: report
  - `platform_tools.py`: vendored adb
  - `search.py`: `find`
  - `cli.py`: command line
- `skills/`: Claude Code skills
- `.claude-plugin/`: plugin and marketplace manifests
