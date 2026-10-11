# Rebuild Decision Matrix

This reference helps the agent decide what to ask and how to interpret the analysis results
before rebuilding. Use after Phase 2 (Extract & RE) completes, during the Phase 3 checkpoint.

## Reading the analysis

### From report.json

| Field | What it tells you | Impact on rebuild |
|---|---|---|
| `inventory.unity_versions` | Which Unity version(s) the assets were built with | User needs this exact version installed |
| `scripting_backend` | `mono` or `il2cpp` | Determines whether scripts can be fully decompiled |
| `inventory.scene_files` | List of `levelN` scene names | These are the game's scenes in build order |
| `il2cpp.status` | `ok`, `error`, or absent | If `error`: metadata may be encrypted |
| `packing_verdict` | `normal`, `likely_packed_or_encrypted`, `inconclusive` | If packed: method bodies are not statically recoverable |
| `native_libraries` | List of .so files | Third-party native plugins to include |

### From extracted-assets (if package step ran)

| Path | What it tells you |
|---|---|
| `catalog.json` | Every exported object with category and output path |
| `assets/textures/` | All decoded textures as PNG |
| `assets/audio/` | All decoded audio as WAV |
| `assets/prefabs/` | GameObject hierarchy trees as JSON |
| `scenes/` | Scene hierarchy trees as JSON |
| `scripts/monoscripts.tsv` | MonoBehaviour script-to-class mapping |

## What to report to the user

### Always report
1. Unity version (and whether they need to install it)
2. Scripting backend
3. Asset counts (textures, audio, meshes, prefabs, scenes)
4. Whether the game uses IL2CPP with encrypted/packed binaries

### Report if relevant
- Render pipeline (URP/HDRP/Built-in) -- detectable from shader keywords
- Wwise/FMOD audio middleware
- Third-party SDKs visible in MonoScript table or IL2CPP names (PlayFab, Firebase, Photon, etc.)
- Obfuscated names (count from dump-il2cpp output)

## Questions to ask

### Mandatory questions

1. **Rebuild scope** -- determines how much work to do:
   - Local only: stub all external calls, no backend, fastest path to "opens in editor"
   - Backend stubs: create service interfaces with stub implementations
   - Full: attempt to recreate all systems (most manual work)

2. **Priority focus** -- determines what to do first:
   - UI/Scenes: focus on visual structure, scene setup, prefab hierarchy
   - Game logic: focus on scripts, state machines, game systems
   - Assets only: just import everything, no script work

3. **Unity version confirmation** -- user may want a different version

### Conditional questions

Ask these only when the analysis reveals them:

| Condition | Question to ask |
|---|---|
| Multiple SDKs detected (PlayFab, Firebase, Photon, etc.) | Which services did the game use? Do you want stubs for all of them? |
| Encrypted metadata or packed binary | Do you want to attempt decryption/unpacking? (warn: needs external tools) |
| >1000 MonoBehaviour scripts | Do you want all scripts, or just specific namespaces? |
| Game uses Wwise/FMOD | Do you want the audio converted to standard Unity AudioClips? |
| Multiple ABIs (arm64, armv7) | Which architecture to prioritize for native plugins? |

### DO NOT ask

- Questions you can answer from the analysis data
- Technical implementation details (the agent decides these)
- Whether to proceed with steps that are prerequisites (just do them)
- Permission to run `uare` commands (the user already asked for a rebuild)

## Interpreting user answers

### "Local only" scope

- Stub all `UnityEngine.Networking`, `PlayFab*`, `Firebase*`, `Photon*` references
- Replace HTTP calls with `Debug.LogWarning("Stubbed: <original call>")`
- Remove or stub certificate pinning, license checks, ad SDKs
- Goal: project opens in Unity, scripts compile, scenes load

### "Backend stubs" scope

- Create interface classes: `IAuthService`, `ILeaderboardService`, `ISaveService`, etc.
- Implement with `Debug.Log` and hardcoded test data
- Preserve the original call sites but route through the stub interfaces
- Goal: project compiles and runs locally with fake data

### "Full reconstruction" scope

- Preserve all scripts as-is (decompiled or stub)
- Create TODO list of systems that need reimplementation
- Set up project structure for gradual replacement
- Goal: complete project structure, even if many parts need manual work

### "UI/Scenes" focus

- Prioritize: scene conversion, prefab hierarchy, Canvas setup, UI sprites
- Lower priority: game logic scripts, audio, native plugins

### "Game logic" focus

- Prioritize: script decompilation/stubs, MonoBehaviour analysis, state machines
- Lower priority: texture quality, audio conversion, UI polish

### "Assets only" focus

- Skip script handling entirely
- Focus on: texture/audio/mesh import, StreamingAssets, materials
- Generate minimal scripts only if needed for compilation
