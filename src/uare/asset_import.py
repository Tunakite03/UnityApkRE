"""Generate Unity .meta files for exported assets so they can be imported into a Unity project.

Each asset and folder in a Unity project needs a `.meta` file with a GUID and importer settings.
GUIDs are derived deterministically from the relative path so re-runs produce stable results.
"""

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError
from uare.logs import event

LOG = logging.getLogger("uare.asset_import")

_FOLDER_META = """\
fileFormatVersion: 2
guid: {guid}
folderAsset: yes
DefaultImporter:
  externalObjects: {{}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_TEXTURE_META = """\
fileFormatVersion: 2
guid: {guid}
TextureImporter:
  internalIDToNameTable: []
  externalObjects: {{}}
  serializedVersion: 12
  mipmaps:
    mipMapMode: 0
    enableMipMap: 1
    sRGBTexture: 1
    linearTexture: 0
    fadeOut: 0
    borderMipMap: 0
  bumpmap:
    convertToNormalMap: 0
    externalNormalMap: 0
    heightScale: 0.25
    normalMapFilter: 0
  isReadable: 1
  streamingMipmaps: 0
  grayScaleToAlpha: 0
  generateCubemap: 6
  seamlessCubemap: 0
  textureFormat: 1
  maxTextureSize: 2048
  textureSettings:
    serializedVersion: 2
    filterMode: 1
    aniso: 1
    mipBias: 0
    wrapU: 0
    wrapV: 0
    wrapW: 0
  nPOTScale: 1
  lightmap: 0
  compressionQuality: 50
  spriteMode: 0
  spriteExtrude: 1
  spriteMeshType: 1
  alignment: 0
  spritePivot: {{x: 0.5, y: 0.5}}
  spritePixelsToUnits: 100
  spriteBorder: {{x: 0, y: 0, z: 0, w: 0}}
  alphaUsage: 1
  alphaIsTransparency: 0
  textureType: 0
  textureShape: 1
  singleChannelComponent: 0
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_SPRITE_META = """\
fileFormatVersion: 2
guid: {guid}
TextureImporter:
  internalIDToNameTable: []
  externalObjects: {{}}
  serializedVersion: 12
  mipmaps:
    mipMapMode: 0
    enableMipMap: 0
    sRGBTexture: 1
  isReadable: 1
  textureFormat: 1
  maxTextureSize: 2048
  textureSettings:
    serializedVersion: 2
    filterMode: 1
    aniso: 1
    mipBias: 0
    wrapU: 1
    wrapV: 1
    wrapW: 0
  spriteMode: 1
  spriteExtrude: 1
  spriteMeshType: 1
  alignment: 0
  spritePivot: {{x: 0.5, y: 0.5}}
  spritePixelsToUnits: 100
  spriteBorder: {{x: 0, y: 0, z: 0, w: 0}}
  alphaUsage: 1
  alphaIsTransparency: 1
  textureType: 8
  textureShape: 1
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_AUDIO_META = """\
fileFormatVersion: 2
guid: {guid}
AudioImporter:
  externalObjects: {{}}
  serializedVersion: 7
  defaultSettings:
    loadType: 0
    sampleRateSetting: 0
    sampleRateOverride: 44100
    compressionFormat: 1
    quality: 1
    conversionMode: 0
    preloadAudioData: 1
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_FONT_META = """\
fileFormatVersion: 2
guid: {guid}
TrueTypeFontImporter:
  externalObjects: {{}}
  serializedVersion: 4
  fontSize: 16
  forceTextureCase: -2
  characterSpacing: 0
  characterPadding: 1
  includeFontData: 1
  fontNames: []
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_MESH_META = """\
fileFormatVersion: 2
guid: {guid}
ModelImporter:
  externalObjects: {{}}
  serializedVersion: 22200
  materials:
    materialImportMode: 2
    materialName: 0
    materialSearch: 1
  meshes:
    globalScale: 1
    meshCompression: 0
    addColliders: 0
    useSRGBMaterialColor: 1
    importVisibility: 1
    importBlendShapes: 1
    importCameras: 1
    importLights: 1
    nodeNameCollisionStrategy: 1
    isReadable: 1
  animation:
    importAnimation: 1
    importConstraints: 0
    importAnimatedCustomProperties: 0
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_SHADER_META = """\
fileFormatVersion: 2
guid: {guid}
ShaderImporter:
  externalObjects: {{}}
  defaultTextures: []
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_SCRIPT_META = """\
fileFormatVersion: 2
guid: {guid}
MonoImporter:
  externalObjects: {{}}
  serializedVersion: 2
  defaultReferences: []
  executionOrder: 0
  icon: {{instanceID: 0}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_ANIM_META = """\
fileFormatVersion: 2
guid: {guid}
NativeFormatImporter:
  externalObjects: {{}}
  mainObjectFileID: 7400000
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_VIDEO_META = """\
fileFormatVersion: 2
guid: {guid}
VideoClipImporter:
  externalObjects: {{}}
  serializedVersion: 2
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_DEFAULT_META = """\
fileFormatVersion: 2
guid: {guid}
DefaultImporter:
  externalObjects: {{}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_PLUGIN_META = """\
fileFormatVersion: 2
guid: {guid}
PluginImporter:
  externalObjects: {{}}
  serializedVersion: 2
  iconMap: {{}}
  executionOrder: {{}}
  defineConstraints: []
  isPreloaded: 0
  isOverridable: 1
  isExplicitlyReferenced: 0
  validateReferences: 1
  platformData:
  - first:
      Any:
    second:
      enabled: 1
      settings: {{}}
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_MATERIAL_META = """\
fileFormatVersion: 2
guid: {guid}
NativeFormatImporter:
  externalObjects: {{}}
  mainObjectFileID: 2100000
  userData:
  assetBundleName:
  assetBundleVariant:
"""

_EXT_TO_TEMPLATE: dict[str, str] = {
    ".png": _TEXTURE_META,
    ".jpg": _TEXTURE_META,
    ".jpeg": _TEXTURE_META,
    ".tga": _TEXTURE_META,
    ".bmp": _TEXTURE_META,
    ".psd": _TEXTURE_META,
    ".wav": _AUDIO_META,
    ".mp3": _AUDIO_META,
    ".ogg": _AUDIO_META,
    ".aiff": _AUDIO_META,
    ".flac": _AUDIO_META,
    ".ttf": _FONT_META,
    ".otf": _FONT_META,
    ".obj": _MESH_META,
    ".fbx": _MESH_META,
    ".dae": _MESH_META,
    ".gltf": _MESH_META,
    ".glb": _MESH_META,
    ".shader": _SHADER_META,
    ".cginc": _SHADER_META,
    ".hlsl": _SHADER_META,
    ".glsl": _SHADER_META,
    ".cs": _SCRIPT_META,
    ".anim": _ANIM_META,
    ".mp4": _VIDEO_META,
    ".webm": _VIDEO_META,
    ".mov": _VIDEO_META,
    ".dll": _PLUGIN_META,
    ".so": _PLUGIN_META,
    ".mat": _MATERIAL_META,
}

_SPRITE_DIRS = frozenset({"sprites", "Sprites"})


@dataclass(frozen=True)
class MetaResult:
    root: str
    files_processed: int
    meta_written: int
    folders_processed: int


def guid_for(relative_path: str) -> str:
    return hashlib.md5(relative_path.encode("utf-8")).hexdigest()


def meta_content(relative_path: str, is_dir: bool, parent_dir_name: str = "") -> str:
    guid = guid_for(relative_path)
    if is_dir:
        return _FOLDER_META.format(guid=guid)
    ext = Path(relative_path).suffix.lower()
    if ext == ".png" and parent_dir_name in _SPRITE_DIRS:
        return _SPRITE_META.format(guid=guid)
    template = _EXT_TO_TEMPLATE.get(ext, _DEFAULT_META)
    return template.format(guid=guid)


def generate_meta_tree(source_dir: Path, out_dir: Path | None = None) -> MetaResult:
    """Write .meta files for every file and folder under source_dir.

    If out_dir is None, .meta files are written alongside the originals.  Otherwise
    a parallel tree of .meta files is created under out_dir.
    """
    if not source_dir.is_dir():
        raise UareError(f"{source_dir} is not a directory")
    target = out_dir or source_dir
    files = folders = meta = 0
    for path in sorted(source_dir.rglob("*")):
        if path.name.endswith(".meta"):
            continue
        rel = path.relative_to(source_dir).as_posix()
        meta_path = target / (path.relative_to(source_dir).as_posix() + ".meta")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        content = meta_content(rel, path.is_dir(), path.parent.name)
        meta_path.write_text(content, encoding="utf-8")
        meta += 1
        if path.is_dir():
            folders += 1
        else:
            files += 1
    event(LOG, logging.INFO, "meta_generated", files=files, folders=folders, meta=meta)
    return MetaResult(root=str(source_dir), files_processed=files, meta_written=meta,
                      folders_processed=folders)
