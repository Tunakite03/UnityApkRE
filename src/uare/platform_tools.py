"""Vendored Android platform-tools: download, verify, and locate the adb binary.

The repository ships adb under vendor/platform-tools/<platform>/ so the toolkit works
without an Android SDK install. `uare fetch-adb` (re)creates those directories from
Google's official platform-tools archives and records provenance in FETCHED.json.

adb resolution order (first match wins, an invalid explicit choice is an error):
    1. --adb <path>
    2. UARE_ADB environment variable
    3. vendor/platform-tools/<host platform>/adb[.exe]
    4. adb on PATH
"""

import hashlib
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.platform_tools")

PLATFORMS = ("darwin", "linux", "windows")
DOWNLOAD_URL = "https://dl.google.com/android/repository/platform-tools-latest-{platform}.zip"
MAX_ARCHIVE_BYTES = 200 << 20
MAX_MEMBER_BYTES = 100 << 20
MAX_CONNECTIONS = 50
DEFAULT_VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "platform-tools"
_ZIP_PREFIX = "platform-tools/"
_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-\d+/(\d+)")
_COMMON_FILES = ("NOTICE.txt", "source.properties")
_PLATFORM_FILES: dict[str, tuple[str, ...]] = {
    "darwin": ("adb",),
    "linux": ("adb",),
    "windows": ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"),
}
_SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",  # macOS, Alpine
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
)


class PlatformToolsError(UareError):
    """Downloading, unpacking, or locating platform-tools failed."""


@dataclass(frozen=True)
class ResolvedAdb:
    path: str
    source: str  # --adb | UARE_ADB | vendored | PATH


@dataclass(frozen=True)
class FetchRecord:
    platform: str
    url: str
    archive_sha256: str
    archive_size: int
    revision: str
    fetched_at: str
    files: dict[str, str]  # name -> sha256


def host_platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    raise PlatformToolsError(f"no platform-tools build for host platform {sys.platform!r}")


def adb_binary_name(platform: str) -> str:
    if platform not in PLATFORMS:
        raise PlatformToolsError(f"unknown platform {platform!r}; expected one of {PLATFORMS}")
    return "adb.exe" if platform == "windows" else "adb"


def vendored_adb(vendor_root: Path, platform: str) -> Path:
    return vendor_root / platform / adb_binary_name(platform)


def _usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def path_lookup(name: str, path_value: str) -> str | None:
    """Like shutil.which, but only absolute PATH entries: never the current directory (Windows default)."""
    for entry in path_value.split(os.pathsep):
        if entry and os.path.isabs(entry) and _usable(Path(entry) / name):
            return str(Path(entry) / name)
    return None


def verify_vendored(adb: Path) -> None:
    """Refuse to run a vendored adb whose sha256 differs from the FETCHED.json written by fetch-adb."""
    record_path = adb.parent / "FETCHED.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        expected = record["files"][adb.name]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PlatformToolsError(f"{record_path} missing or unreadable; re-run `uare fetch-adb`: {exc}") from exc
    actual = _sha256(adb)
    if actual != expected:
        raise PlatformToolsError(f"{adb} sha256 {actual} != FETCHED.json {expected}; vendored adb was modified")


def resolve_adb(explicit: str | None, environ: Mapping[str, str], vendor_root: Path, platform: str,
                which: Callable[[str], str | None]) -> ResolvedAdb:
    if explicit is not None:
        if not _usable(Path(explicit)):
            raise PlatformToolsError(f"--adb {explicit} is not an executable file")
        return ResolvedAdb(explicit, "--adb")
    env_value = environ.get("UARE_ADB")
    if env_value:
        if not _usable(Path(env_value)):
            raise PlatformToolsError(f"UARE_ADB={env_value} is not an executable file")
        return ResolvedAdb(env_value, "UARE_ADB")
    bundled = vendored_adb(vendor_root, platform)
    if _usable(bundled):
        verify_vendored(bundled)
        return ResolvedAdb(str(bundled), "vendored")
    on_path = which(adb_binary_name(platform))
    if on_path is not None:
        return ResolvedAdb(on_path, "PATH")
    raise PlatformToolsError(
        f"adb not found: no --adb, no UARE_ADB, nothing at {bundled}, nothing on PATH. Run `uare fetch-adb`."
    )


def parse_revision(source_properties: str) -> str:
    for line in source_properties.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "Pkg.Revision":
            return value.strip()
    raise PlatformToolsError("source.properties has no Pkg.Revision")


def ssl_context(environ: Mapping[str, str]) -> ssl.SSLContext:
    """Default context, falling back to the OS CA bundle when Python ships without one (python.org macOS)."""
    if environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context()
    default = ssl.get_default_verify_paths()
    if (default.cafile and Path(default.cafile).is_file()) or (default.capath and any(Path(default.capath).glob("*"))):
        return ssl.create_default_context()
    for bundle in _SYSTEM_CA_BUNDLES:
        if Path(bundle).is_file():
            event(LOG, logging.INFO, "ca_bundle_fallback", cafile=bundle)
            return ssl.create_default_context(cafile=bundle)
    raise PlatformToolsError("no CA bundle found; set SSL_CERT_FILE to a PEM bundle")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _total_size(response: "urllib.response.addinfourl", resumed_from: int) -> int:
    """Full archive size from Content-Range (206) or Content-Length (200)."""
    content_range = response.headers.get("Content-Range")
    if resumed_from and response.status == 206 and content_range:
        match = _CONTENT_RANGE_RE.fullmatch(content_range.strip())
        if match is None:
            raise PlatformToolsError(f"malformed Content-Range {content_range!r}")
        start, total = int(match.group(1)), int(match.group(2))
        if start != resumed_from:
            raise PlatformToolsError(f"server resumed at byte {start}, expected {resumed_from}")
        return total
    length = response.headers.get("Content-Length")
    if length is None or not length.strip().isdigit():
        raise PlatformToolsError(f"server sent no valid Content-Length ({length!r}); cannot verify completeness")
    return int(length)


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never from https to a weaker scheme."""

    def redirect_request(self, req: urllib.request.Request, fp: object, code: int, msg: str,
                         headers: object, newurl: str) -> "urllib.request.Request | None":
        if req.full_url.startswith("https://") and not newurl.startswith("https://"):
            raise PlatformToolsError(f"refusing redirect from {req.full_url} to non-https {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


def download(url: str, target: Path, context: ssl.SSLContext, retries: int) -> None:
    """Stream url into target, resuming with HTTP Range after a truncated connection.

    `retries` bounds consecutive attempts without progress; MAX_CONNECTIONS bounds the total.
    """
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _HttpsOnlyRedirect())
    received = 0
    stalled = 0
    last: Exception | None = None
    target.write_bytes(b"")
    for _connection in range(MAX_CONNECTIONS):
        headers = {"Range": f"bytes={received}-"} if received else {}
        progress_before = received
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
                if received and response.status != 206:
                    event(LOG, logging.WARNING, "range_ignored", url=url, status=response.status)
                    received = 0
                    target.write_bytes(b"")
                total = _total_size(response, received)
                if total > MAX_ARCHIVE_BYTES:
                    raise PlatformToolsError(f"{url} is {total} bytes, above the {MAX_ARCHIVE_BYTES} cap")
                with target.open("ab") as out:
                    for chunk in iter(lambda: response.read(1 << 20), b""):
                        received += len(chunk)
                        if received > total:
                            raise PlatformToolsError(f"{url} sent more than the declared {total} bytes")
                        out.write(chunk)
            if received == total:
                return
            last = urllib.error.URLError(f"connection closed at {received} of {total} bytes")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        stalled = 0 if received > progress_before else stalled + 1
        if stalled > retries:
            raise PlatformToolsError(f"download of {url} stalled at {received} bytes: {last}") from last
        event(LOG, logging.WARNING, "download_resume", url=url, received=received, error=str(last))
        time.sleep(1 + stalled)
    raise PlatformToolsError(f"download of {url} needed more than {MAX_CONNECTIONS} connections; giving up")


def unpack(archive: Path, platform: str, dest: Path) -> dict[str, str]:
    """Copy adb (+ Windows DLLs), NOTICE.txt and source.properties from the archive into dest."""
    wanted = _PLATFORM_FILES[platform] + _COMMON_FILES
    hashes: dict[str, str] = {}
    try:
        with zipfile.ZipFile(archive) as zf:
            for name in wanted:
                try:
                    info = zf.getinfo(_ZIP_PREFIX + name)
                except KeyError as exc:
                    raise PlatformToolsError(f"{archive.name} lacks {_ZIP_PREFIX + name}") from exc
                if info.file_size > MAX_MEMBER_BYTES:
                    raise PlatformToolsError(f"{info.filename} expands to {info.file_size} bytes, above the cap")
                target = dest / name
                with zf.open(info) as src, target.open("wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        out.write(chunk)
                if name in ("adb", "adb.exe"):
                    target.chmod(0o755)
                hashes[name] = _sha256(target)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PlatformToolsError(f"cannot unpack {archive}: {exc}") from exc
    return hashes


def adb_version(adb: str) -> str:
    try:
        result = subprocess.run([adb, "version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlatformToolsError(f"cannot run {adb} version: {exc}") from exc
    if result.returncode != 0:
        raise PlatformToolsError(f"{adb} version exited {result.returncode}: {result.stderr.strip()}")
    return " / ".join(line.strip() for line in result.stdout.splitlines()[:2])


def official_url(platform: str) -> str:
    adb_binary_name(platform)
    return DOWNLOAD_URL.format(platform=platform)


def fetch_platform(platform: str, url: str, vendor_root: Path, expected_sha256: str | None,
                   context: ssl.SSLContext) -> FetchRecord:
    """Download a platform-tools zip for `platform` into vendor_root/<platform> (must be absent or empty)."""
    adb_binary_name(platform)
    with staged_output(vendor_root / platform) as staging:
        archive = staging / "platform-tools.zip"
        event(LOG, logging.INFO, "download_start", url=url)
        download(url, archive, context, retries=2)
        archive_sha = _sha256(archive)
        if expected_sha256 is not None and archive_sha != expected_sha256.lower():
            raise PlatformToolsError(f"{url} sha256 {archive_sha} != expected {expected_sha256}")
        size = archive.stat().st_size
        files = unpack(archive, platform, staging)
        archive.unlink()
        revision = parse_revision((staging / "source.properties").read_text(encoding="utf-8"))
        if platform == host_platform():
            event(LOG, logging.INFO, "adb_verified", version=adb_version(str(staging / adb_binary_name(platform))))
        record = FetchRecord(platform, url, archive_sha, size, revision,
                             datetime.now(timezone.utc).isoformat(), files)
        (staging / "FETCHED.json").write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8")
    event(LOG, logging.INFO, "platform_tools_vendored", platform=platform, revision=revision, sha256=archive_sha)
    return record
