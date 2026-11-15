# Original asset paths and identities

A build keeps an asset's original project path only in a few places. Place an asset at an original path only
with one of the sources below, cite the source, and leave everything else in an explicitly unmapped folder.
A plausible folder is not evidence.

## Evidence, strongest first

| Source | Gives | How to read it |
|---|---|---|
| AssetBundle `m_Container` key that is an `Assets/...` path (path-named builds) | path of the bundle's main assets | `package` / `blueprint` read it directly |
| Addressables catalog (`catalog_*.json`) | asset GUID -> exact path (case kept), for every addressable entry | `--addressables-catalog` on `package`, `blueprint`, `scene-spec`, `assetripper-remap`; container keys are then GUIDs |
| `globalgamemanagers` BuildSettings `scenes` (or `m_Scenes`) | path of each player scene; `levelN` is entry N | read automatically by `blueprint` and `scene-spec` |
| AssetBundle `m_SceneHashes` + catalog | path of a streamed (Addressables) scene | the key is the scene GUID |
| `ResourceManager.m_Container` in `globalgamemanagers` | Resources-relative path (lower case, no extension) of every `Resources` asset | the parent folder of `Resources/` is not recorded |
| Serialized file named by an asset GUID (`Data/<guid>`) | that asset's GUID, not its path | `blueprint` verifies the layout with package GUID anchors |
| AssetRipper file named `<guid>.<ext>` (streamed scenes, shaders) + catalog | that asset's path | AssetRipper uses the container GUID as the name when it has no other; `assetripper-remap --addressables-catalog` moves it |
| Sprite `m_RenderDataKey` | GUID (and file ID) of the sprite's source texture | `--split-atlases` uses it; the path still needs the catalog |
| Content-identical copy of an asset that has one of the above | the same path and GUID | see "Duplicates" |

Name-only matches are not evidence. Many games reuse generic names (`skeleton_xs`, `Icon`, `Bg`) across dozens of
different assets; a name match picked the wrong copy in practice where a content hash found the right one.

## Where player-data assets come from

| Serialized file | Holds | Original path |
|---|---|---|
| `sharedassetsN.assets` | dependencies of player scene N, packed at build time | not recorded; only a duplicate or a catalog entry can name it |
| `Data/<guid>` files listed in `ResourceManager.m_Container` | `Resources` assets | `<unknown parent>/Resources/<recorded path>` |
| `resources.assets` | `Resources` assets in builds that use it | as above |
| `unity default resources`, `unity_builtin_extra` | Unity's built-in resources | none: reference the Editor's built-in, never copy |
| `globalgamemanagers(.assets)` | project settings and SDK settings objects | not recorded |

Do not conclude anything about `Resources` from a missing `resources.assets`: check `ResourceManager.m_Container`.
A folder named `_Resources` (or any other name) is an ordinary folder; only folders named exactly `Resources`
are special. Scene dependencies from `sharedassetsN` never belong in `Resources`: moving them there changes
what the build includes and how it loads them.

## Duplicates

The same source asset can be built twice: into `sharedassetsN` (a player scene uses it) and into an Addressables
bundle (the catalog names it). Identical content proves the common source:

- textures: decoded pixel hash; fonts and text: byte hash; materials: shader keywords and every saved float, color
  and texture slot name; clips: name, sample rate, stop time and events.
- When exactly one copy has a catalog identity, give every copy that path and GUID and keep one file
  (`assetripper-remap --addressables-catalog` does this for kinds with a fixed main-object file ID; prefabs are
  never merged because their internal file IDs differ).
- When several copies have different catalog identities, or none has one, keep them apart and report them.

## Built-in resources

Objects in `unity default resources` / `unity_builtin_extra`, and copies of built-in extras that bundles carry
by name (`Default-Skybox`, `Sprites-Default`, `Default-Material`, ...), are referenced, not imported:
GUID `0000000000000000f000000000000000` (default resources) or `0000000000000000e000000000000000` (built-in
extra) with the original file ID. AssetRipper already writes them this way; the scene builder resolves
`builtin` spec rows through the Editor's resources.

## Placement rules

1. Recorded path (any source above): use it, keep the exported file's extension (`.psd` -> `.png`,
   `.fbx` -> `.prefab`) and restore the GUID the same source gives.
2. Content-identical to a recorded asset: same as 1, merged into one file.
3. `Resources` asset: `Assets/Resources/<recorded path>` is a reconstruction choice; report that the parent
   folder is unknown.
4. Built-in: reference the built-in, no file.
5. Anything else: an explicitly unmapped folder (`Assets/_Unmapped/AssetRipper/<type>/` for AssetRipper exports,
   `Assets/_Unmapped/AtlasSprites/<atlas>/` for split sprites, or the scene builder's `<assetsFolder>/Shared`). Report the count; never invent a folder from a name, a plugin's
   default install path or a neighbouring asset.

## Checks before claiming a path

- Count assets per evidence class with explicit denominators (placed by catalog, merged duplicates, Resources,
  built-in, unmapped).
- For every merged duplicate, keep the content comparison that justified it (`mapping.json` evidence).
- Re-run the counts after each remap; a path that is not in the evidence above stays unmapped.
