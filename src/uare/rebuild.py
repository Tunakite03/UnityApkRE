"""Orchestrate a full Unity project rebuild from a completed ``uare run`` workdir.

This is the P0 module that ties all reconstruction steps together. Given a work
directory that contains the outputs of ``uare run`` (and optionally ``dump-il2cpp``,
``il2cpp-enrich``, ``decompile``, etc.), it creates a Unity project directory that
can be opened in the Unity Editor.

The output has the standard Unity project layout::

    ProjectSettings/
        ProjectVersion.txt
        ProjectSettings.asset     (minimal)
    Packages/
        manifest.json             (URP/built-in depending on analysis)
    Assets/
        Textures/
        Audio/
        Meshes/
        Materials/
        Fonts/
        Scripts/                  (Mono decompiled or IL2CPP stubs)
        Shaders/
        Scenes/
        Prefabs/
        Resources/                (other assets)
        Plugins/                  (native .so files)
        StreamingAssets/           (raw streaming data)
        Wwise/                    (Wwise WEM audio if present)
"""

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.rebuild")

_PROJECT_VERSION = """\
m_EditorVersion: {version}
m_EditorVersionWithRevision: {version} (000000000000)
"""

_MINIMAL_PROJECT_SETTINGS = """\
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
"""

_PACKAGES_MANIFEST = """\
{
  "dependencies": {
    "com.unity.ugui": "1.0.0"
  }
}
"""


@dataclass(frozen=True)
class RebuildResult:
    unity_version: str
    scripting_backend: str
    project_dir: str
    textures: int
    audio: int
    meshes: int
    scripts: int
    shaders: int
    scenes: int
    prefabs: int
    native_plugins: int
    other_assets: int
    steps_run: tuple[str, ...]
    warnings: tuple[str, ...]


_CATEGORY_DIRS: dict[str, str] = {
    "textures": "Assets/Textures",
    "audio": "Assets/Audio",
    "meshes": "Assets/Meshes",
    "materials": "Assets/Materials",
    "fonts": "Assets/Fonts",
    "scripts": "Assets/Scripts",
    "animations": "Assets/Animations",
    "videos": "Assets/Videos",
    "prefabs": "Assets/Prefabs",
}

_TEXTURE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tga", ".bmp", ".psd"})
_AUDIO_EXTS = frozenset({".wav", ".mp3", ".ogg", ".aiff", ".flac"})
_MESH_EXTS = frozenset({".obj", ".fbx", ".dae", ".gltf", ".glb"})
_SCRIPT_EXTS = frozenset({".cs"})
_SHADER_EXTS = frozenset({".shader", ".cginc", ".hlsl", ".glsl"})


def _count_by_ext(directory: Path, exts: frozenset[str]) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for f in directory.rglob("*") if f.is_file() and f.suffix.lower() in exts)


def _copy_tree(src: Path, dst: Path) -> int:
    """Copy src tree into dst. Returns count of files copied."""
    if not src.is_dir():
        return 0
    count = 0
    for item in sorted(src.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def _load_report(workdir: Path) -> dict:
    report_path = workdir / "analysis" / "report.json"
    if not report_path.is_file():
        raise UareError(f"{report_path} not found; run `uare run` or `uare analyze` first")
    return json.loads(report_path.read_text(encoding="utf-8"))


def _detect_unity_version(report: dict) -> str:
    versions = report.get("inventory", {}).get("unity_versions", {})
    if versions:
        return max(versions, key=versions.get)
    return "2021.3.0f1"


def _write_project_settings(project: Path, unity_version: str) -> None:
    settings_dir = project / "ProjectSettings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "ProjectVersion.txt").write_text(
        _PROJECT_VERSION.format(version=unity_version), encoding="utf-8")
    (settings_dir / "ProjectSettings.asset").write_text(
        _MINIMAL_PROJECT_SETTINGS, encoding="utf-8")


def _write_packages(project: Path) -> None:
    packages_dir = project / "Packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    (packages_dir / "manifest.json").write_text(_PACKAGES_MANIFEST, encoding="utf-8")


def _copy_extracted_assets(assets_dir: Path, project: Path) -> dict[str, int]:
    """Copy categorized assets from ``uare package`` output into Unity project layout."""
    counts: dict[str, int] = {}
    for category, target_rel in _CATEGORY_DIRS.items():
        src = assets_dir / "assets" / category
        if src.is_dir():
            target = project / target_rel
            count = _copy_tree(src, target)
            counts[category] = count
    other_src = assets_dir / "assets"
    if other_src.is_dir():
        known_categories = set(_CATEGORY_DIRS.keys())
        for sub in sorted(other_src.iterdir()):
            if sub.is_dir() and sub.name not in known_categories:
                target = project / "Assets" / "Resources" / sub.name
                count = _copy_tree(sub, target)
                counts.setdefault("other", 0)
                counts["other"] = counts.get("other", 0) + count
    return counts


def _copy_scenes(assets_dir: Path, project: Path) -> int:
    src = assets_dir / "scenes"
    if not src.is_dir():
        return 0
    return _copy_tree(src, project / "Assets" / "Scenes")


def _copy_native_plugins(extracted_dir: Path, project: Path) -> int:
    lib_dir = extracted_dir / "lib"
    if not lib_dir.is_dir():
        return 0
    count = 0
    for abi_dir in sorted(lib_dir.iterdir()):
        if not abi_dir.is_dir():
            continue
        for so_file in sorted(abi_dir.glob("*.so")):
            if so_file.name.startswith("libil2cpp") or so_file.name.startswith("libunity"):
                continue
            target = project / "Assets" / "Plugins" / "Android" / abi_dir.name / so_file.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(so_file, target)
            count += 1
    return count


def _copy_streaming_assets(extracted_dir: Path, project: Path) -> int:
    for candidate in (extracted_dir / "assets", extracted_dir / "Data" / "StreamingAssets"):
        sa = candidate / "StreamingAssets" if candidate.name != "StreamingAssets" else candidate
        if sa.is_dir():
            return _copy_tree(sa, project / "Assets" / "StreamingAssets")
    return 0


def _run_mono_decompile(extracted_dir: Path, project: Path, warnings: list[str]) -> int:
    managed = extracted_dir / "Data" / "Managed"
    if not managed.is_dir():
        warnings.append("Managed/ directory not found; skipping Mono decompilation")
        return 0
    try:
        from uare.mono_decompile import decompile_all, resolve_decompiler
        import os
        decompiler = resolve_decompiler(None, os.environ, Path(__file__).resolve().parents[2] / "vendor" / "ilspy",
                                        lambda n: shutil.which(n))
        scripts_dir = project / "Assets" / "Scripts"
        result = decompile_all(managed, scripts_dir, decompiler)
        event(LOG, logging.INFO, "mono_decompiled", assemblies=result.decompiled, files=result.total_cs_files)
        return result.total_cs_files
    except Exception as exc:
        warnings.append(f"Mono decompilation failed: {exc}")
        return 0


def _run_il2cpp_merge(workdir: Path, project: Path, warnings: list[str]) -> int:
    il2cpp_dir = workdir / "il2cpp"
    if not il2cpp_dir.is_dir() or not (il2cpp_dir / "types.tsv").is_file():
        il2cpp_dir = workdir / "analysis" / "il2cpp"
        if not il2cpp_dir.is_dir():
            warnings.append("No IL2CPP dump found; skipping annotated stub generation")
            return 0
    enrich_dir = workdir / "enrich" if (workdir / "enrich").is_dir() else None
    ghidra_dir = workdir / "ghidra" if (workdir / "ghidra").is_dir() else None
    try:
        from uare.il2cpp_merge import merge
        scripts_dir = project / "Assets" / "Scripts" / "IL2CPP"
        result = merge(il2cpp_dir, enrich_dir, ghidra_dir, scripts_dir)
        event(LOG, logging.INFO, "il2cpp_merged", types=result.types, methods=result.methods_total)
        return result.files_written
    except Exception as exc:
        warnings.append(f"IL2CPP merge failed: {exc}")
        return 0


def _run_shader_recovery(assets_dir: Path, project: Path, warnings: list[str]) -> int:
    try:
        from uare.shader_recover import recover_shaders
        shaders_dir = project / "Assets" / "Shaders"
        result = recover_shaders(assets_dir, shaders_dir)
        event(LOG, logging.INFO, "shaders_recovered_rebuild",
              original=result.recovered_original, reconstructed=result.recovered_parsed)
        return result.recovered_original + result.recovered_parsed
    except Exception as exc:
        warnings.append(f"Shader recovery failed: {exc}")
        return 0


def _run_scene_convert(assets_dir: Path, project: Path, warnings: list[str]) -> tuple[int, int]:
    try:
        from uare.scene_convert import convert_scenes_and_prefabs
        scenes_dir = project / "Assets" / "Scenes"
        result = convert_scenes_and_prefabs(assets_dir, scenes_dir)
        return result.scenes_converted, result.prefabs_converted
    except Exception as exc:
        warnings.append(f"Scene/prefab conversion failed: {exc}")
        return 0, 0


def _run_meta_generation(project: Path, warnings: list[str]) -> int:
    assets_root = project / "Assets"
    if not assets_root.is_dir():
        return 0
    try:
        from uare.asset_import import generate_meta_tree
        result = generate_meta_tree(assets_root)
        event(LOG, logging.INFO, "meta_generated_rebuild", meta=result.meta_written)
        return result.meta_written
    except Exception as exc:
        warnings.append(f".meta generation failed: {exc}")
        return 0


def _run_wwise_extract(extracted_dir: Path, project: Path, warnings: list[str]) -> int:
    bnk_dirs = [extracted_dir / "assets", extracted_dir / "Data" / "StreamingAssets"]
    for d in bnk_dirs:
        if d.is_dir() and list(d.rglob("*.bnk")):
            try:
                from uare.wwise import extract_all
                wwise_out = project / "Assets" / "Wwise"
                result = extract_all(d, wwise_out)
                return result.total_wems
            except Exception as exc:
                warnings.append(f"Wwise extraction failed: {exc}")
                return 0
    return 0


def rebuild(workdir: Path, out_dir: Path) -> RebuildResult:
    """Create a Unity project from a completed ``uare run`` work directory."""
    if not workdir.is_dir():
        raise UareError(f"{workdir} is not a directory")

    report = _load_report(workdir)
    unity_version = _detect_unity_version(report)
    backend = report.get("scripting_backend", "unknown")

    extracted_dir = workdir / "extracted"
    assets_dir = workdir / "extracted-assets"
    warnings: list[str] = []
    steps: list[str] = []

    with staged_output(out_dir) as project:
        _write_project_settings(project, unity_version)
        steps.append("project_settings")

        _write_packages(project)
        steps.append("packages_manifest")

        asset_counts: dict[str, int] = {}
        if assets_dir.is_dir():
            asset_counts = _copy_extracted_assets(assets_dir, project)
            steps.append("copy_assets")
        elif extracted_dir.is_dir():
            warnings.append("No extracted-assets/ directory; run `uare package` for full asset export")

        scenes_copied = _copy_scenes(assets_dir, project)
        if scenes_copied:
            steps.append("copy_scenes")

        native_count = 0
        if extracted_dir.is_dir():
            native_count = _copy_native_plugins(extracted_dir, project)
            if native_count:
                steps.append("native_plugins")
            _copy_streaming_assets(extracted_dir, project)

        scripts = 0
        if backend == "mono":
            scripts = _run_mono_decompile(extracted_dir, project, warnings)
            if scripts:
                steps.append("mono_decompile")
        elif backend == "il2cpp":
            scripts = _run_il2cpp_merge(workdir, project, warnings)
            if scripts:
                steps.append("il2cpp_merge")

        shaders = _run_shader_recovery(assets_dir, project, warnings)
        if shaders:
            steps.append("shader_recovery")

        scenes_conv, prefabs_conv = _run_scene_convert(assets_dir, project, warnings)
        if scenes_conv or prefabs_conv:
            steps.append("scene_convert")

        wwise_count = _run_wwise_extract(extracted_dir, project, warnings)
        if wwise_count:
            steps.append("wwise_extract")

        meta_count = _run_meta_generation(project, warnings)
        if meta_count:
            steps.append("meta_generation")

        rebuild_report = {
            "unity_version": unity_version,
            "scripting_backend": backend,
            "steps": steps,
            "assets": asset_counts,
            "scripts": scripts,
            "shaders": shaders,
            "scenes_converted": scenes_conv,
            "prefabs_converted": prefabs_conv,
            "native_plugins": native_count,
            "wwise_wems": wwise_count,
            "meta_files": meta_count,
            "warnings": warnings,
        }
        (project / "rebuild_report.json").write_text(
            json.dumps(rebuild_report, indent=2, ensure_ascii=False), encoding="utf-8")

    event(LOG, logging.INFO, "rebuild_done", version=unity_version, backend=backend,
          steps=len(steps), warnings=len(warnings))

    return RebuildResult(
        unity_version=unity_version,
        scripting_backend=backend,
        project_dir=str(out_dir),
        textures=asset_counts.get("textures", 0),
        audio=asset_counts.get("audio", 0),
        meshes=asset_counts.get("meshes", 0),
        scripts=scripts,
        shaders=shaders,
        scenes=scenes_conv,
        prefabs=prefabs_conv,
        native_plugins=native_count,
        other_assets=asset_counts.get("other", 0),
        steps_run=tuple(steps),
        warnings=tuple(warnings),
    )


def summarize(result: RebuildResult) -> list[str]:
    lines = [
        f"unity version     : {result.unity_version}",
        f"scripting backend : {result.scripting_backend}",
        f"project dir       : {result.project_dir}",
        f"textures          : {result.textures}",
        f"audio             : {result.audio}",
        f"meshes            : {result.meshes}",
        f"scripts           : {result.scripts}",
        f"shaders           : {result.shaders}",
        f"scenes            : {result.scenes}",
        f"prefabs           : {result.prefabs}",
        f"native plugins    : {result.native_plugins}",
        f"other assets      : {result.other_assets}",
        f"steps run         : {', '.join(result.steps_run)}",
    ]
    if result.warnings:
        lines.append(f"warnings          : {len(result.warnings)}")
        for w in result.warnings:
            lines.append(f"  - {w}")
    return lines
