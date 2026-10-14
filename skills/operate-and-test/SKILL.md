---
name: operate-and-test
description: Guide an agent to operate and test the uare toolkit without operating a device. Reads a dry-run plan (`uare plan`), runs the offline unit tests, and checks produced outputs against their invariants. Use when the user says /unity-apk-re:operate-and-test, "how do I run this without the phone", "plan the pipeline", "test the toolkit", "check the extracted assets", or asks an agent to verify an existing output directory.
argument-hint: '<package id> --workdir <new dir> | <existing workdir>'
allowed-tools: Read, Glob, Grep, Bash
---

# Operate and test uare without touching a device

This skill plans, tests and verifies; it does not run the pipeline. When the user asks to
analyze or rebuild a game, use the `analyze-apk` or `rebuild-game` skill instead, which run the
pipeline without a separate confirmation.

Call the scripts by full path: `"${CLAUDE_PLUGIN_ROOT}/scripts/uare"` and
`"${CLAUDE_PLUGIN_ROOT}/scripts/run-tests"` (in a checkout, the repository root). Below,
`scripts/...` means those paths.

## 1. Plan before running

```bash
scripts/uare plan <package> --workdir <new dir> --json
```

The plan is read-only: it checks adb resolution, whether UnityPy, Pillow and fmod_toolkit import, and
whether the work directory is free. It never contacts a device and never creates the directory.

Read the JSON and report:

- `ready`: `false` means `blockers` lists what to fix first. Quote the blocker text.
- `steps[].needs_device`: only `pull` needs a phone. Do not run it unless the user asked for it.
- `steps[].needs_unitypy`: `package` needs the `power` extra. Create `<plugin-root>/.venv-power` as described in [the tool guide](../../docs/agent-tools.md) (the wrapper uses it automatically), or point `UARE_PYTHON` at an interpreter that has it. Never `pip install uare[...]`: that is a different PyPI package.
- `steps[].command` and `steps[].writes`: the exact command for each step and the output it produces.

If `ready` is `true` and the user did not ask to pull, stop here and present the plan.

## 2. Offline tests (no device, no real data)

```bash
scripts/run-tests
```

The suite uses fake UnityPy, Pillow and fmod_toolkit modules, so it runs on a plain Python 3.10+
interpreter. Expected result: `OK`. Skips are expected for the real-data smoke test and for tests
that need real UnityPy/Pillow; report the actual `skipped=` count and their reasons (`-v`).

To include it after an analysis of a real build:

```bash
UARE_REAL_DATA_DIR=<workdir>/extracted/Data scripts/run-tests
```

Expected result: `OK`, with the real-data smoke test no longer skipped. Use `UARE_PYTHON` to pick
the interpreter.

## 3. Verify an existing output (no re-run)

These checks read files that already exist. They do not run the pipeline.

- `manifest.json` at the work directory root: `status` must be `complete`, or `partial` with `status_reason` that the user accepts. `failed` names the stage in `stages[].error`. `scope.decrypted` and `scope.decompiled` are empty because `run` itself never decrypts; decryption
runs as separate commands with their own outputs (`recovery.json`, `keys.json`, `report.json`).
- `manifest.json` `outputs[].sha256`: recompute the hash of each listed file and compare. A mismatch means the file changed after the run.

- `report.json` in the analysis directory: `scripts/uare summary <workdir>/analysis` prints the summary.
- `extracted-assets/report.json`: `errors` is an integer count; investigate every nonzero count.
- `extracted-assets/catalog.json`: every entry needs an `output` or a `note`. An entry with neither is a bug.
- Each `output` path in the catalog exists on disk. No two objects share one output (components and monoscripts are the only shared outputs, by design).
- `categories` in `report.json` counts the files written. `in_hierarchy` counts the components stored inside prefab and scene JSON.
- Each scene, prefab and spine folder has a `manifest.json`. Every `assets[].path` in it exists and is a hard link of its `shared` file: same inode, link count of at least 2.

A small check that does all of these:

```bash
python3 - "<workdir>/extracted-assets" <<'PY'
import json, os, sys, collections
root = sys.argv[1]
catalog = json.load(open(os.path.join(root, "catalog.json")))
claimed = collections.defaultdict(set)
for entry in catalog:
    if entry["output"] and entry["category"] not in ("components", "monoscripts"):
        claimed[entry["output"]].add((entry["source"], entry["serialized_file"], entry["path_id"]))
    assert entry["output"] or entry["note"], entry
    if entry["output"]:
        assert os.path.exists(os.path.join(root, entry["output"])), entry["output"]
assert all(len(v) == 1 for v in claimed.values()), "an output is claimed by two objects"
print(f"ok: {len(catalog)} catalog entries, {len(claimed)} unique outputs")
PY
```

The path in quotes is the first argument; replace `<workdir>` with the work directory.

## 4. When a step fails

| Error | Meaning | What to tell the user |
|---|---|---|
| `DeviceSelectionError` | no phone, or several phones without `--serial` | connect the phone, authorise USB debugging, or pass `--serial` |
| `UnityPyRequired` | the package step cannot import UnityPy, Pillow or fmod_toolkit | create `<plugin-root>/.venv-power` ([tool guide](../../docs/agent-tools.md)) or set `UARE_PYTHON` |
| `OutputDirectoryError` | the output directory is not empty | use a new directory; outputs are never overwritten |
| `PlatformToolsError` | no usable adb | `scripts/uare fetch-adb`, or set `UARE_ADB` |

## Rules for an agent

- Within this skill, do not run `pull` or `run` against a device; the analysis skills do that.
- Reuse an existing capture for follow-up analysis; each producing command needs
  a new or empty output directory. A fresh `run` requires a new workspace. Do not
  create another competing game root just to retry one stage. See
  [workspace and tool guidance](../../docs/agent-tools.md).
- Decryption (`recover-metadata`, `unpack-native`, `decrypt-hotdll`) is allowed; verify its outputs
  like any other stage. The toolkit never patches, re-signs or reinstalls a build.
- Report exactly what the command printed, including errors. Do not claim a check passed unless you ran it.

## Reconstruction verification

The run has five stages, ending in `blueprint`. Pipeline `complete` does not mean
a complete game. Inspect `blueprint/report.json` coverage and missing references,
then use `blueprint/index.html` to follow hierarchy, script/animation bindings and
asset usage. Rebuild results are structural drafts. Check
`Reconstruction/export_blockers.json` and record Unity import, compilation and
Play Mode separately. Do not mark any of them passed merely because YAML exists.
