"""Native Unity asset export through a vendored AssetRipper, driven over its headless web API.

AssetRipper (`AssetRipper.GUI.Free --headless --port N`) writes a Unity project with native assets:
prefabs, AnimationClips, AnimatorControllers and override controllers, materials, meshes, fonts, and
textures whose import settings carry the original sprites (`SpriteExportMode=Native`: Single/Multiple
sheets with rects, pivots, borders and pixels per unit). An extracted APK tree is presented to it in the
Android layout (`assets/bin/Data`, `lib/`) through symlinks so it recognises the IL2CPP backend.

Script fields need the IL2CPP type information; when AssetRipper cannot initialise it (protected or
scrambled metadata) it falls back to the Unknown backend and exports MonoBehaviours without fields. That
outcome is read from its log and recorded in report.json; `uare package --script-assemblies` remains the
source for script fields. AssetRipper assigns its own GUIDs and file IDs: they are not original identities.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from uare.analyze import resolve_layout
from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.assetripper")

DEFAULT_VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "assetripper" / "app"
_EXECUTABLES = ("AssetRipper.GUI.Free", "AssetRipper.GUI.Free.exe")
# Fixed export settings: native sprites (sprite sheets in texture importers) and deterministic text/image formats.
SETTINGS: dict[str, str] = {
    "BundledAssetsExportMode": "DirectExport", "ScriptContentLevel": "Level2", "AudioExportFormat": "Default",
    "ImageExportFormat": "Png", "LightmapTextureExportFormat": "Yaml", "SpriteExportMode": "Native",
    "ShaderExportMode": "Dummy", "TextExportMode": "Parse", "ScriptLanguageVersion": "AutoSafe",
    "ScriptExportMode": "Hybrid", "DefaultVersion": "0.0.0a0", "TargetVersion": "0.0.0a0",
    "PreferOriginalTextureExtension": "true",
}
_LOADED = "Finished processing assets"
_EXPORTED = "Finished post-export"


class AssetRipperError(UareError):
    """AssetRipper is unavailable or its export did not complete."""


@dataclass(frozen=True)
class ResolvedAssetRipper:
    path: str
    source: str


@dataclass(frozen=True)
class AssetRipperResult:
    tool: str
    tool_source: str
    input_layout: str
    settings: dict[str, str]
    scripting_backend: str
    script_fields_exported: bool
    errors: tuple[str, ...]
    folders: dict[str, int]
    out_dir: str


def _usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_assetripper(explicit: str | None, environ: Mapping[str, str], vendor_root: Path,
                        which: Callable[[str], str | None]) -> ResolvedAssetRipper:
    if explicit is not None:
        if not _usable(Path(explicit)):
            raise AssetRipperError(f"--assetripper {explicit} is not an executable file")
        return ResolvedAssetRipper(explicit, "--assetripper")
    env_value = environ.get("UARE_ASSETRIPPER")
    if env_value:
        if not _usable(Path(env_value)):
            raise AssetRipperError(f"UARE_ASSETRIPPER={env_value} is not an executable file")
        return ResolvedAssetRipper(env_value, "UARE_ASSETRIPPER")
    for name in _EXECUTABLES:
        if _usable(vendor_root / name):
            return ResolvedAssetRipper(str(vendor_root / name), "vendored")
    for name in _EXECUTABLES:
        found = which(name)
        if found:
            return ResolvedAssetRipper(found, "PATH")
    raise AssetRipperError("no AssetRipper found: pass --assetripper, set UARE_ASSETRIPPER, or vendor it with "
                           "`uare fetch-tool --dest vendor/assetripper/app`")


def android_layout(root: Path, target: Path) -> Path:
    """Symlink an extracted tree into the Android APK layout AssetRipper recognises; returns `target`."""
    layout = resolve_layout(root)
    if layout.lib_dir is None:
        raise AssetRipperError(f"Extracted tree has no lib/ directory for the IL2CPP backend; root={root}")
    (target / "assets" / "bin").mkdir(parents=True)
    (target / "assets" / "bin" / "Data").symlink_to(layout.data_dir.resolve(), target_is_directory=True)
    (target / "lib").symlink_to(layout.lib_dir.resolve(), target_is_directory=True)
    if layout.assets_dir is not None:
        for entry in sorted(layout.assets_dir.iterdir()):
            if entry.name != "bin":
                (target / "assets" / entry.name).symlink_to(entry.resolve(), target_is_directory=entry.is_dir())
    return target


def read_log(text: str) -> tuple[str, bool, tuple[str, ...]]:
    """Scripting backend in effect, whether script fields were exportable, and the error lines of a run."""
    backends = re.findall(r"Files use the '(\w+)' scripting backend", text)
    fallback = "Switching to the 'Unknown' scripting backend" in text
    backend = "Unknown" if fallback else (backends[-1] if backends else "not_reported")
    errors = tuple(line for line in text.splitlines() if "[Error]" in line)
    return backend, backend in {"IL2Cpp", "Mono"}, errors


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    # Loopback only: an HTTP(S)_PROXY from the environment must never see these requests.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *handlers)


def _post(base: str, route: str, fields: Mapping[str, str], timeout: float) -> None:
    data = urllib.parse.urlencode(dict(fields)).encode()
    request = urllib.request.Request(base + route, data=data, method="POST")
    try:
        with _opener(_NoRedirect()).open(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code  # the API answers a completed form post with a redirect
    except (urllib.error.URLError, OSError) as exc:
        raise AssetRipperError(f"AssetRipper {route} request failed: {exc}") from exc
    if status not in (200, 302):
        raise AssetRipperError(f"AssetRipper {route} failed; status={status}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _wait_ready(base: str, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssetRipperError(f"AssetRipper exited before serving; code={process.returncode}")
        try:
            with _opener().open(base + "/openapi.json", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise AssetRipperError(f"AssetRipper did not start serving within {timeout:.0f}s")


def _require_log(log: Path, marker: str, step: str, grace: float = 30.0) -> str:
    """The log text once `marker` is written; the request has returned, so only buffered output can lag."""
    deadline = time.monotonic() + grace
    while True:
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        if marker in text:
            return text
        if time.monotonic() >= deadline:
            raise AssetRipperError(f"AssetRipper {step} did not complete; log={log}")
        time.sleep(0.5)


def export_project(root: Path, out_dir: Path, tool: ResolvedAssetRipper, timeout: float = 7200.0) -> AssetRipperResult:
    """Export `root` (an extracted APK tree) as an AssetRipper Unity project into `out_dir` (new or empty)."""
    with staged_output(out_dir) as staging:
        source = android_layout(root, staging / ".input")
        log = staging / "assetripper.log"
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        process = subprocess.Popen([tool.path, "--headless", "--port", str(port), "--log-path", str(log)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _wait_ready(base, process, 120.0)
            _post(base, "/Settings/Update", SETTINGS, 60.0)
            _post(base, "/LoadFolder", {"Path": str(source)}, timeout)
            _require_log(log, _LOADED, "load")
            _post(base, "/Export/UnityProject", {"Path": str(staging / "export")}, timeout)
            text = _require_log(log, _EXPORTED, "export")
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)
        shutil.rmtree(staging / ".input")
        backend, fields, errors = read_log(text)
        assets = staging / "export" / "ExportedProject" / "Assets"
        folders = {p.name: sum(1 for f in p.rglob("*") if f.is_file() and f.suffix != ".meta")
                   for p in sorted(assets.iterdir()) if p.is_dir()} if assets.is_dir() else {}
        result = AssetRipperResult(tool.path, tool.source, "android (assets/bin/Data + lib symlinks)", dict(SETTINGS),
                                   backend, fields, errors, folders, str(out_dir))
        (staging / "report.json").write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    event(LOG, logging.INFO, "assetripper_exported", backend=backend, script_fields=fields, errors=len(errors))
    return result


def summarize(result: AssetRipperResult) -> list[str]:
    return [
        f"assetripper       : {result.tool} [{result.tool_source}]",
        f"scripting backend : {result.scripting_backend} (script fields exported: {result.script_fields_exported})",
        f"errors in log     : {len(result.errors)}",
        "exported folders  : " + ", ".join(f"{k}={v}" for k, v in result.folders.items()),
        f"output            : {result.out_dir}/export (AssetRipper GUIDs and file IDs, not original identities)",
    ]
