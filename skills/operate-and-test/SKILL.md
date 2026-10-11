---
name: operate-and-test
description: Guide an agent to operate and test the uare toolkit without operating a device. Reads a dry-run plan (`uare plan`), runs the offline unit tests, and checks produced outputs against their invariants. Use when the user says /unity-apk-re:operate-and-test, "how do I run this without the phone", "plan the pipeline", "test the toolkit", "check the extracted assets", or asks an agent to verify an existing output directory.
argument-hint: '<package id> --workdir <new dir> | <existing workdir>'
allowed-tools: Read, Glob, Grep, Bash
---

# Operate and test uare without touching a device

The default for an agent is to **describe and verify**, not to run the pipeline. Pulling from a
phone, writing a new work directory and exporting assets are actions the user has to ask for
explicitly. This skill gives the agent the plan, the offline checks and the verification steps.

Resolve the wrapper relative to this skill directory: `scripts/uare` and `scripts/run-tests` at the plugin root.

## 1. Plan before running

```bash
scripts/uare plan <package> --workdir <new dir> --json
```

The plan is read-only: it checks adb resolution, whether UnityPy, Pillow and fmod_toolkit import, and
whether the work directory is free. It never contacts a device and never creates the directory.

Read the JSON and report:

- `ready`: `false` means `blockers` lists what to fix first. Quote the blocker text.
- `steps[].needs_device`: only `pull` needs a phone. Do not run it unless the user asked for it.
- `steps[].needs_unitypy`: `package` needs the `power` extra. Install with `pip install "uare[power]"`, or point `UARE_PYTHON` at an interpreter that has it.
- `steps[].command` and `steps[].writes`: the exact command for each step and the output it produces.

If `ready` is `true` and the user did not ask to pull, stop here and present the plan.

## 2. Offline tests (no device, no real data)

```bash
scripts/run-tests
```

The suite uses fake UnityPy, Pillow and fmod_toolkit modules, so it runs on a plain Python 3.10+
interpreter. Expected result: `OK` with `skipped=1`. The skipped test is the real-data smoke test.

To include it after an analysis of a real build:

```bash
UARE_REAL_DATA_DIR=<workdir>/extracted/Data scripts/run-tests
```

Expected result: `OK` with no skip. Use `UARE_PYTHON` to pick the interpreter.

## 3. Verify an existing output (no re-run)

These checks read files that already exist. They do not run the pipeline.

- `manifest.json` at the work directory root: `status` must be `complete`, or `partial` with `status_reason` that the user accepts. `failed` names the stage in `stages[].error`. `scope.decrypted` and `scope.decompiled` are always empty.
- `manifest.json` `outputs[].sha256`: recompute the hash of each listed file and compare. A mismatch means the file changed after the run.

- `report.json` in the analysis directory: `scripts/uare summary <workdir>/analysis` prints the summary.
- `extracted-assets/report.json`: `errors` is an integer count; investigate every nonzero count.
- `extracted-assets/catalog.json`: every entry needs an `output` or a `note`. An entry with neither is a bug.
- Each `output` path in the catalog exists on disk. No two objects share one output (components and monoscripts are the only shared outputs, by design).
- `categories` in `report.json` counts the files written. `in_hierarchy` counts the components stored inside prefab and scene JSON.

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
| `UnityPyRequired` | the package step cannot import UnityPy, Pillow or fmod_toolkit | `pip install "uare[power]"` or set `UARE_PYTHON` |
| `OutputDirectoryError` | the output directory is not empty | use a new directory; outputs are never overwritten |
| `PlatformToolsError` | no usable adb | `scripts/uare fetch-adb`, or set `UARE_ADB` |

## Rules for an agent

- Do not run `pull` or `run` against a device unless the user asked for it in this conversation.
- Reuse an existing capture for follow-up analysis; each producing command needs
  a new or empty output directory. A fresh `run` requires a new workspace. Do not
  create another competing game root just to retry one stage. See
  [workspace and tool guidance](../../docs/agent-tools.md).
- Do not decrypt, patch or re-sign anything. Hot-update assemblies stay as stored; the toolkit does not decrypt them.
- Report exactly what the command printed, including errors. Do not claim a check passed unless you ran it.

## Reconstruction verification

The run has five stages, ending in `blueprint`. Pipeline `complete` does not mean
a complete game. Inspect `blueprint/report.json` coverage and missing references,
then use `blueprint/index.html` to follow hierarchy, script/animation bindings and
asset usage. Rebuild results are structural drafts. Check
`Reconstruction/export_blockers.json` and record Unity import, compilation and
Play Mode separately. Do not mark any of them passed merely because YAML exists.
