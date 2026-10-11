# Reconstruction-oriented architecture

Status: implemented evidence model and structural reconstruction pipeline.
This document describes shipped behavior, not a promise of complete source recovery.

## Data flow

`run` executes `pull → extract → analyze → package → blueprint`.
`package` captures scoped objects and raw serialized fields while the UnityPy
objects are available. `blueprint` joins this model with analysis, script metadata,
SDK signatures and optional behavior/runtime evidence. `rebuild` consumes the
same model and produces a structural draft plus an explicit blocker report.

## Canonical model

`extracted-assets/game_model.json` uses schema `uare.game-model`, version 1.
Object IDs hash `(source container path, serialized file name, path ID)`.
File records preserve the external-file table. References retain the source
object, field path, external file index and target path ID. Status is one of
`resolved`, `null`, `missing_file`, `missing_object`, `invalid_external_index`,
or `ambiguous`. Ambiguous filenames are not resolved by guessing.

Object data is `complete`, `partial`, or `unreadable`, with decoder issues.
Typed-read fallback preserves known fields while retaining the type-tree failure.
Byte fields are base64 tagged. No field-name prefix filter discards gameplay data.
The persisted references are recomputed from validated fields when loading.
JSON is the exchange format; no database dependency is required.

## Blueprint outputs

- `report.json`: coverage, scene/prefab scopes, dependencies, script inventory,
  behavior claims, observations, limits and validation state.
- `scopes/<id>.json`: hierarchy, component bindings, direct/transitive assets,
  animations, scene settings, serialized events and unresolved references.
- `game_model.json`: canonical source evidence and references.
- `overview.md`: compact human-readable entry point.
- `index.html`: self-contained offline browser with search, object fields,
  references and reverse usage. Untrusted values use text nodes, not HTML.

A level filename is scene evidence with an index candidate. A `.prefab` container
entry is prefab evidence. Other object trees remain unclassified. BuildSettings
can supply scene names. The report does not invent runtime transitions.
Animation objects retain serialized parameters, states, clips and events where
the decoder exposes them; controller semantics and rig compatibility remain
unverified. Static reference closure does not cover dynamic or remote loads.

## Behavior analysis

The plugin skill directs the agent to analyze code and observed behavior after
structural extraction. Class names and serialized events alone do not establish
method bodies, win/loss conditions, progression, saving or service behavior.

Optional `behavior.json` contains `claims`, each with `scope_id`, `system`,
`statement`, `basis` (`code_analysis` or `inference`) and `citations`. Citations
contain a workdir-relative `path`, one-based `start_line` and `end_line`.
The loader validates containment and line bounds and fingerprints the cited text.
The interpretation still needs review and behavioral verification.

Optional `observations.json` contains `observations`, each with `scope_id`,
`action`, `result` and `evidence`. Use `game` or a scope ID from the report.
Observations are labeled supplied, not independently verified.

Dependency signatures use MonoScript names, metadata identifiers and library
filenames. They prove presence only. Exact original package versions and runtime
service initialization are unknown unless independently established.

## Structural export

`rebuild` requires the canonical model. Legacy category-only exports fail with
instructions to repackage; they do not silently take the old reconstruction path.
The exporter allocates local file IDs before writing references, preserves
RectTransform and custom fields, and maps supported media and uniquely matched
source scripts to deterministic GUIDs. Unknown bindings are nulled only in the
draft and enumerated in `Reconstruction/export_blockers.json`.

`Reconstruction/Blueprint` retains the specification and evidence.
`Reconstruction/bindings.json` records generated bindings. IL2CPP annotations
are code evidence outside `Assets`, not executable recovered implementations.
Unsupported native assets (including controller/rig reconstruction) remain
explicit work. Original importer settings and shader behavior need validation.

`rebuild-packages.json` can select explicit registry versions through a
`dependencies` object. These versions are reconstruction choices. Without it,
the manifest is empty with a warning; the tool never guesses package versions.

## Verification contract

Tests exercise cross-file identities, ambiguous/missing/null references, graph
cycles, prefab provenance, transitive animation/asset usage, evidence validation,
HTML escaping, custom fields, RectTransform and GUID bindings. Run:

```sh
scripts/run-tests
UARE_REAL_DATA_DIR=/path/to/Unity/Data scripts/run-tests
```

Command completion is distinct from reconstruction completeness. Generated
projects report `structural_draft`; Unity import, compilation and Play Mode remain
`not_tested` until independently exercised in a matching Editor. A full fidelity
check still requires a known-source project built to APK and comparison of its
recovered structures and gameplay scenarios.
