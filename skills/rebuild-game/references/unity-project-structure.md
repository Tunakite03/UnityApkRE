# Generated reconstruction layout

```text
ProjectSettings/
  ProjectVersion.txt
  ProjectSettings.asset
Packages/
  manifest.json
Assets/
  Scenes/
  Prefabs/
  Scripts/
  Textures/
  Audio/
  Meshes/
  Fonts/
  Shaders/
  Resources/
  Plugins/Android/
  StreamingAssets/
Reconstruction/
  Blueprint/
    index.html
    report.json
    game_model.json
    scopes/
  CodeEvidence/IL2CPP/
  bindings.json
  export_blockers.json
rebuild_report.json
```

ProjectVersion contains the selected detected version, without an invented
revision hash. Player settings are draft defaults, not recovered project settings.
The report records unverified regional version normalization where applicable.

Package versions are never derived from SDK names. An optional workdir-level
`rebuild-packages.json` supplies explicit registry versions under `dependencies`.
Without it, the manifest is empty with a warning.

The canonical model remains outside `Assets`. Raw type-tree JSON and IL2CPP
annotations are evidence, not executable scripts or native Unity assets.
Supported structural scenes preserve component fields and RectTransform and
remap local IDs. Generated media/source-script references use the same
path-derived GUIDs as `.meta` generation. Unknown bindings are recorded as
blockers. Material, shader, animation/controller, rig and importer fidelity need
class-specific reconstruction and Editor verification.

Do not import player engine binaries or IL2CPP metadata as project assets.
Do not mark the project complete until import, compilation and behavioral checks
have actually run and the remaining gaps are explicit.
