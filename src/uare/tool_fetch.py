"""Download and unpack an external reverse-engineering tool into vendor/.

The generic sibling of `fetch-adb`: `uare fetch-tool --url <archive> --dest vendor/<name>` streams
an archive into a staging directory, verifies an optional sha256, and extracts it with the same
guards used elsewhere (no absolute paths, no `..`, per-member and total size caps). A non-zip
download is stored as a single file. Provenance lands in FETCHED.json.
"""

import hashlib
import json
import logging
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output
from uare.platform_tools import ssl_context  # re-exported convenience

LOG = logging.getLogger("uare.tool_fetch")

DEFAULT_MAX_ARCHIVE = 2 << 30  # 2 GiB (Ghidra is large)
DEFAULT_MAX_MEMBER = 1 << 30
_CHUNK = 1 << 20


class ToolFetchError(UareError):
    """Downloading or unpacking an external tool failed."""


@dataclass(frozen=True)
class ToolRecord:
    name: str
    url: str
    archive_sha256: str
    archive_size: int
    fetched_at: str
    files: dict[str, str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path, context, max_bytes: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "uare"})
    try:
        with urllib.request.urlopen(request, timeout=60, context=context) as response:
            length = response.headers.get("Content-Length")
            if length is not None and length.isdigit() and int(length) > max_bytes:
                raise ToolFetchError(f"{url} is {length} bytes, above the {max_bytes} cap")
            received = 0
            with target.open("wb") as out:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_bytes:
                        raise ToolFetchError(f"{url} sent more than the {max_bytes} byte cap")
                    out.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        raise ToolFetchError(f"download of {url} failed: {exc}") from exc


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    for info in zf.infolist():
        name = Path(info.filename)
        if name.is_absolute() or ".." in name.parts:
            raise ToolFetchError(f"archive member {info.filename!r} escapes the destination")
    return zf.infolist()


def fetch_archive(url: str, dest: Path, *, expected_sha256: str | None = None, name: str | None = None,
                  context=None, max_archive: int = DEFAULT_MAX_ARCHIVE,
                  max_member: int = DEFAULT_MAX_MEMBER) -> ToolRecord:
    context = context or ssl_context({})
    label = name or dest.name
    with staged_output(dest) as staging:
        archive = staging / "download.bin"
        event(LOG, logging.INFO, "tool_download_start", url=url, dest=str(dest))
        _download(url, archive, context, max_archive)
        archive_sha = _sha256(archive)
        if expected_sha256 is not None and archive_sha != expected_sha256.lower():
            raise ToolFetchError(f"{url} sha256 {archive_sha} != expected {expected_sha256}")
        size = archive.stat().st_size
        files: dict[str, str] = {}
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                total = 0
                for info in _safe_members(zf):
                    total += info.file_size
                    if info.file_size > max_member or total > max_archive:
                        raise ToolFetchError(f"archive expands beyond the size cap at {info.filename!r}")
                    target = staging / info.filename
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, target.open("wb") as out:
                        for chunk in iter(lambda: src.read(_CHUNK), b""):
                            out.write(chunk)
                    files[info.filename] = _sha256(target)
            archive.unlink()
        else:
            stored = staging / (Path(url).name or "tool.bin")
            stored.write_bytes(archive.read_bytes())
            if dest.name and stored.name != dest.name:
                stored = stored.rename(staging / dest.name)
            files[stored.name] = archive_sha
            archive.unlink()
        record = ToolRecord(name=label, url=url, archive_sha256=archive_sha, archive_size=size,
                            fetched_at=datetime.now(timezone.utc).isoformat(), files=files)
        (staging / "FETCHED.json").write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8")
    event(LOG, logging.INFO, "tool_fetched", tool=label, sha256=archive_sha, files=len(record.files))
    return record
