# Unity Project Structure Reference

When creating a Unity project from extracted assets, follow this exact layout.
Unity Editor expects these directories and files to exist.

## Required files

### ProjectSettings/ProjectVersion.txt

```
m_EditorVersion: 2022.3.10f1
m_EditorVersionWithRevision: 2022.3.10f1 (000000000000)
```

Replace the version with the one detected from `report.json` → `inventory.unity_versions`
(pick the most common one). The revision hash `000000000000` is a placeholder -- Unity
Editor ignores it on project open.

### ProjectSettings/ProjectSettings.asset

Minimal valid PlayerSettings:

```yaml
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!129 &1
PlayerSettings:
  m_ObjectHideFlags: 0
  serializedVersion: 22
  productGUID: 00000000000000000000000000000000
  AndroidBundleVersionCode: 1
  productName: RecoveredProject
  defaultScreenWidth: 1920
  defaultScreenHeight: 1080
  companyName: Recovered
  enableNativePlatformBackendsForNewInputSystem: 1
```

### Packages/manifest.json

Minimal package manifest. Add render pipeline packages if the game uses URP/HDRP
(detectable from shader keywords or `UnityEngine.Rendering.Universal` references).

```json
{
  "dependencies": {
    "com.unity.ugui": "1.0.0"
  }
}
```

If URP detected, add:
```json
{
  "dependencies": {
    "com.unity.ugui": "1.0.0",
    "com.unity.render-pipelines.universal": "14.0.0"
  }
}
```

Version numbers should match the detected Unity version's default package versions.

## Asset directory structure

```
Assets/
  Textures/       ← .png, .jpg, .tga, .bmp, .psd
  Audio/          ← .wav, .mp3, .ogg, .aiff, .flac
  Meshes/         ← .obj, .fbx, .dae, .gltf, .glb
  Materials/      ← .mat files
  Fonts/          ← .ttf, .otf
  Animations/     ← .anim, animation clips
  Videos/         ← .mp4, .webm, .mov
  Scripts/        ← .cs files (decompiled or stubs)
  Shaders/        ← .shader, .cginc, .hlsl
  Scenes/         ← .unity scene files
  Prefabs/        ← .prefab files
  Resources/      ← catch-all for uncategorized assets
  Plugins/
    Android/
      arm64-v8a/  ← third-party .so files (NOT libil2cpp/libunity)
      armeabi-v7a/
  StreamingAssets/ ← raw data files the game loads at runtime
  Wwise/          ← extracted WEM audio (if game uses Wwise)
```

## .meta files

Every file AND folder under `Assets/` needs a `.meta` sidecar file. Use `uare generate-meta`
to create them, or generate them manually:

- GUID: `md5(relative_path_from_Assets_root)` as 32 hex chars
- Each file type has a specific importer (TextureImporter, AudioImporter, etc.)
- Folders use `DefaultImporter` with `folderAsset: yes`
- `.png` files inside `sprites/` or `Sprites/` directories use `textureType: 8` (Sprite)

See `../../src/uare/asset_import.py` for the complete template set.

## What NOT to include

- `libil2cpp.so` and `libunity.so` -- these are engine binaries, not project assets
- `global-metadata.dat` -- IL2CPP metadata, not a project file
- System/Unity assemblies (`mscorlib.dll`, `UnityEngine.*.dll`) -- Unity provides these
- The `Library/` folder -- Unity regenerates this on first open
