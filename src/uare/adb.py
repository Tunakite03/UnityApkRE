"""adb adapter: device selection, package lookup, and pulling APK splits plus OBB files.

Commands are always passed to adb as an argument list. `adb shell` joins its arguments
into a remote shell command line, so every value interpolated into a shell call is
validated against a strict pattern first. Values read back from the device are
validated before they are used as pull sources, local file names, or printed.
"""

import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from uare.errors import (
    AdbError,
    DeviceSelectionError,
    InvalidPackageNameError,
    PackageNotFoundError,
)
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.adb")

Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]

_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
_REMOTE_APK_RE = re.compile(r"^/[A-Za-z0-9_.~=+/\-]+\.apk$")
_OBB_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.obb$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")
_TRANSIENT_MARKERS = ("device offline", "protocol fault", "connection reset", "closed")
_OBB_ROOT = "/sdcard/Android/obb"


def subprocess_runner(argv: Sequence[str], timeout_s: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_s, check=False)


@dataclass(frozen=True)
class AdbConfig:
    adb_path: str
    serial: str | None
    timeout_s: float
    pull_timeout_s: float
    retries: int
    backoff_s: float

    def __post_init__(self) -> None:
        if self.retries < 0:
            raise AdbError(f"retries must be >= 0, got {self.retries}")
        if self.timeout_s <= 0 or self.pull_timeout_s <= 0:
            raise AdbError(f"timeouts must be positive, got {self.timeout_s} / {self.pull_timeout_s}")


@dataclass(frozen=True)
class Device:
    serial: str
    state: str


@dataclass(frozen=True)
class PackageVersion:
    version_name: str | None
    version_code: str | None


@dataclass(frozen=True)
class PulledFile:
    kind: str  # "apk" | "obb"
    remote_path: str
    local_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PullManifest:
    package: str
    device_serial: str
    version: PackageVersion
    pulled_at: str
    files: tuple[PulledFile, ...]


def validate_package_name(package: str) -> str:
    if not _PACKAGE_RE.fullmatch(package):
        raise InvalidPackageNameError(f"invalid Android package name: {package!r}")
    return package


def parse_devices(output: str) -> list[Device]:
    """Parse `adb devices`: one `<serial>\\t<state>` line per device after the header."""
    devices: list[Device] = []
    for line in output.splitlines():
        if not line.strip() or line.startswith("List of devices") or line.startswith("* "):
            continue
        serial, sep, state = line.partition("\t")
        if not sep or not _SERIAL_RE.fullmatch(serial) or not state.strip().isprintable():
            raise AdbError(f"unrecognised 'adb devices' line: {line!r}")
        devices.append(Device(serial=serial, state=state.strip()))
    return devices


def select_device(devices: Sequence[Device], serial: str | None) -> Device:
    ready = [d for d in devices if d.state == "device"]
    if serial is not None:
        match = [d for d in devices if d.serial == serial]
        if not match:
            raise DeviceSelectionError(f"device {serial!r} not connected; seen: {[d.serial for d in devices]}")
        if match[0].state != "device":
            raise DeviceSelectionError(f"device {serial!r} is in state {match[0].state!r}, expected 'device'")
        return match[0]
    if len(ready) == 1:
        return ready[0]
    if not ready:
        states = {d.serial: d.state for d in devices}
        raise DeviceSelectionError(f"no ready device (need USB debugging authorised); seen: {states}")
    raise DeviceSelectionError(f"multiple ready devices, pass --serial: {[d.serial for d in ready]}")


def parse_pm_path(output: str, package: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("package:"):
            raise AdbError(f"unexpected 'pm path {package}' line: {line!r}")
        remote = line[len("package:"):]
        if not _REMOTE_APK_RE.fullmatch(remote) or ".." in PurePosixPath(remote).parts:
            raise AdbError(f"refusing unexpected remote APK path for {package}: {remote!r}")
        paths.append(remote)
    if not paths:
        raise PackageNotFoundError(f"package {package!r} is not installed on the device")
    return paths


def parse_package_list(output: str) -> list[str]:
    names: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("package:"):
            raise AdbError(f"unexpected 'pm list packages' line: {line!r}")
        names.append(validate_package_name(line[len("package:"):]))
    return sorted(names)


def parse_package_version(output: str) -> PackageVersion:
    name = re.search(r"versionName=([^\s]+)", output)
    code = re.search(r"versionCode=(\d+)", output)
    return PackageVersion(version_name=name.group(1) if name else None, version_code=code.group(1) if code else None)


def parse_obb_listing(output: str) -> tuple[list[str], list[str]]:
    """Split an `ls -1` of the OBB directory into (obb names, ignored entries)."""
    names: list[str] = []
    ignored: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line:
            (names if _OBB_NAME_RE.fullmatch(line) else ignored).append(line)
    return names, ignored


class AdbClient:
    """Thin adapter over the adb binary. Device-scoped calls go through _device; host calls through _host."""

    def __init__(self, config: AdbConfig, runner: Runner) -> None:
        self._config = config
        self._runner = runner
        self._serial: str | None = config.serial

    @property
    def serial(self) -> str:
        if self._serial is None:
            raise DeviceSelectionError("no device selected; call select() first")
        return self._serial

    def _execute(self, argv: list[str], timeout_s: float) -> "subprocess.CompletedProcess[str]":
        attempts = self._config.retries + 1
        last_error = ""
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = self._runner(argv, timeout_s)
            except subprocess.TimeoutExpired as exc:
                last_error, last_exc = f"timed out after {timeout_s}s", exc
            else:
                stderr = (result.stderr or "").strip()
                transient = result.returncode != 0 and any(m in stderr.lower() for m in _TRANSIENT_MARKERS)
                if not transient:
                    return result
                last_error, last_exc = stderr, None
            if attempt < attempts:
                event(LOG, logging.WARNING, "adb_retry", argv=argv[1:], attempt=attempt, error=last_error)
                time.sleep(self._config.backoff_s * attempt)
        raise AdbError(f"adb {' '.join(argv[1:])} failed after {attempts} attempts: {last_error}") from last_exc

    def _host(self, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        return self._execute([self._config.adb_path, *args], self._config.timeout_s)

    def _device(self, args: Sequence[str], timeout_s: float) -> "subprocess.CompletedProcess[str]":
        return self._execute([self._config.adb_path, "-s", self.serial, *args], timeout_s)

    @staticmethod
    def _require_success(args: Sequence[str], result: "subprocess.CompletedProcess[str]") -> str:
        if result.returncode != 0:
            raise AdbError(f"adb {' '.join(args)} exited {result.returncode}: {(result.stderr or result.stdout).strip()}")
        return result.stdout

    def _device_output(self, args: Sequence[str]) -> str:
        return self._require_success(args, self._device(args, self._config.timeout_s))

    def devices(self) -> list[Device]:
        return parse_devices(self._require_success(["devices"], self._host(["devices"])))

    def select(self) -> Device:
        device = select_device(self.devices(), self._config.serial)
        self._serial = device.serial
        return device

    def third_party_packages(self) -> list[str]:
        return parse_package_list(self._device_output(["shell", "pm", "list", "packages", "-3"]))

    def package_paths(self, package: str) -> list[str]:
        validate_package_name(package)
        result = self._device(["shell", "pm", "path", package], self._config.timeout_s)
        if result.returncode != 0 and not result.stdout.strip():
            raise PackageNotFoundError(f"package {package!r} is not installed: {result.stderr.strip()}")
        return parse_pm_path(result.stdout, package)

    def package_version(self, package: str) -> PackageVersion:
        validate_package_name(package)
        return parse_package_version(self._device_output(["shell", "dumpsys", "package", package]))

    def obb_files(self, package: str) -> list[str]:
        validate_package_name(package)
        directory = f"{_OBB_ROOT}/{package}"
        result = self._device(["shell", "ls", "-1", directory], self._config.timeout_s)
        combined = f"{result.stdout}\n{result.stderr}"
        if "No such file or directory" in combined:
            event(LOG, logging.INFO, "obb_absent", package=package, directory=directory)
            return []
        if result.returncode != 0:
            raise AdbError(f"listing {directory} exited {result.returncode}: {combined.strip()}")
        names, ignored = parse_obb_listing(result.stdout)
        if ignored:
            event(LOG, logging.WARNING, "obb_entries_ignored", directory=directory, entries=ignored)
        return [f"{directory}/{name}" for name in names]

    def pull(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        args = ["pull", remote, str(local)]
        self._require_success(args, self._device(args, self._config.pull_timeout_s))
        if not local.is_file():
            raise AdbError(f"adb pull reported success but {local} does not exist")

    def pidof(self, package: str) -> list[int]:
        """Find PIDs of running package processes."""
        validate_package_name(package)
        result = self._device(["shell", "pidof", package], self._config.timeout_s)
        pids: list[int] = []
        if result.returncode == 0 and result.stdout.strip():
            for part in result.stdout.strip().split():
                if part.isdigit():
                    pids.append(int(part))
        return pids

    def clear_logcat(self) -> None:
        """Clear logcat buffer."""
        args = ["logcat", "-c"]
        self._require_success(args, self._device(args, self._config.timeout_s))

    def build_logcat_command(self, *, tags: Sequence[str] | None = None, pid: int | None = None,
                              dump: bool = False, lines: int | None = None,
                              buffer: str | None = "all", format: str = "threadtime") -> list[str]:
        """Construct full adb command line for logcat execution."""
        cmd = [self._config.adb_path, "-s", self.serial, "logcat"]
        if buffer:
            cmd.extend(["-b", buffer])
        cmd.extend(["-v", format])
        if dump:
            cmd.append("-d")
        if lines is not None and lines > 0:
            cmd.extend(["-t", str(lines)])
        if pid is not None:
            cmd.extend(["--pid", str(pid)])
        if tags:
            cmd.extend(tags)
            cmd.append("*:S")
        return cmd

    def logcat(self, *, tags: Sequence[str] | None = None, pid: int | None = None,
               dump: bool = False, lines: int | None = None,
               buffer: str | None = "all", format: str = "threadtime") -> list[str]:
        """Execute logcat and return lines."""
        args = ["logcat"]
        if buffer:
            args.extend(["-b", buffer])
        args.extend(["-v", format])
        if dump:
            args.append("-d")
        if lines is not None and lines > 0:
            args.extend(["-t", str(lines)])
        if pid is not None:
            args.extend(["--pid", str(pid)])
        if tags:
            args.extend(tags)
            args.append("*:S")
        result = self._device(args, self._config.timeout_s)
        self._require_success(args, result)
        return result.stdout.splitlines()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pull_package(client: AdbClient, package: str, out_dir: Path) -> PullManifest:
    """Pull every APK split and OBB of `package` into out_dir/{apks,obb} and write manifest.json."""
    validate_package_name(package)
    with staged_output(out_dir) as staging:
        device = client.select()
        version = client.package_version(package)
        remote_apks = client.package_paths(package)
        remote_obbs = client.obb_files(package)
        event(LOG, logging.INFO, "pull_start", package=package, serial=device.serial,
              version=version.version_name, apks=len(remote_apks), obbs=len(remote_obbs))

        used: set[str] = set()
        pulled: list[PulledFile] = []
        for kind, remotes, sub in (("apk", remote_apks, "apks"), ("obb", remote_obbs, "obb")):
            for remote in remotes:
                name = PurePosixPath(remote).name
                if name in used:
                    raise AdbError(f"two remote files map to the same local name {name!r}")
                used.add(name)
                relative = Path(sub) / name
                client.pull(remote, staging / relative)
                local = staging / relative
                pulled.append(PulledFile(kind=kind, remote_path=remote, local_path=relative.as_posix(),
                                         size=local.stat().st_size, sha256=sha256_file(local)))
                event(LOG, logging.INFO, "pulled", remote=remote, size=pulled[-1].size)

        manifest = PullManifest(package=package, device_serial=device.serial, version=version,
                                pulled_at=datetime.now(timezone.utc).isoformat(), files=tuple(pulled))
        (staging / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2), encoding="utf-8")
    return manifest
