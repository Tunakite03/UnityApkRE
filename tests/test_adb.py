import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from uare.adb import (
    AdbClient,
    AdbConfig,
    Device,
    parse_devices,
    parse_obb_listing,
    parse_pm_path,
    pull_package,
    select_device,
    validate_package_name,
)
from uare.errors import (
    AdbError,
    DeviceSelectionError,
    InvalidPackageNameError,
    OutputDirectoryError,
    PackageNotFoundError,
)

PKG = "com.example.unitygame"
BASE = f"/data/app/~~abc==/{PKG}-xyz==/base.apk"
ABI = f"/data/app/~~abc==/{PKG}-xyz==/split_config.arm64_v8a.apk"


def ok(stdout: str = "", stderr: str = "", code: int = 0) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Scripted adb: maps argument tuples (without adb path and -s serial) to queued results."""

    def __init__(self, script: dict[tuple[str, ...], list["subprocess.CompletedProcess[str]"]],
                 pulled: dict[str, bytes]) -> None:
        self.script = script
        self.pulled = pulled
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout_s: float) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        args = list(argv[1:])
        if args[:1] == ["-s"]:
            args = args[2:]
        if args[0] == "pull":
            Path(args[2]).write_bytes(self.pulled[args[1]])
            return ok(stdout="1 file pulled")
        queue = self.script[tuple(args)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def client(runner: FakeRunner, retries: int) -> AdbClient:
    return AdbClient(AdbConfig("adb", None, 5.0, 5.0, retries, 0.0), runner)


def device_script() -> dict[tuple[str, ...], list["subprocess.CompletedProcess[str]"]]:
    return {
        ("devices",): [ok("List of devices attached\nSER123\tdevice\n\n")],
        ("shell", "dumpsys", "package", PKG): [ok("    versionCode=203 minSdk=24\n    versionName=2.0.3\n")],
        ("shell", "pm", "path", PKG): [ok(f"package:{BASE}\npackage:{ABI}\n")],
        ("shell", "ls", "-1", f"/sdcard/Android/obb/{PKG}"): [
            ok(stderr="ls: /sdcard/Android/obb/x: No such file or directory", code=1)],
    }


class PackageNameTests(unittest.TestCase):
    def test_accepts_application_id(self) -> None:
        self.assertEqual(validate_package_name("com.percent.aos.rollinghero"), "com.percent.aos.rollinghero")

    def test_rejects_shell_metacharacters(self) -> None:
        for bad in ("com.x;reboot", "com.x && id", "nodots", "com..x", "1com.x"):
            with self.subTest(bad=bad), self.assertRaises(InvalidPackageNameError):
                validate_package_name(bad)


class DeviceSelectionTests(unittest.TestCase):
    def test_single_ready_device_is_selected(self) -> None:
        devices = [Device("A", "device"), Device("B", "unauthorized")]
        self.assertEqual(select_device(devices, None).serial, "A")

    def test_multiple_ready_devices_require_serial(self) -> None:
        with self.assertRaises(DeviceSelectionError):
            select_device([Device("A", "device"), Device("B", "device")], None)

    def test_unauthorized_serial_is_rejected(self) -> None:
        with self.assertRaises(DeviceSelectionError):
            select_device([Device("B", "unauthorized")], "B")

    def test_no_devices(self) -> None:
        with self.assertRaises(DeviceSelectionError):
            select_device([], None)


class ParserTests(unittest.TestCase):
    def test_pm_path_lists_splits(self) -> None:
        self.assertEqual(parse_pm_path(f"package:{BASE}\npackage:{ABI}\n", PKG), [BASE, ABI])

    def test_pm_path_empty_means_not_installed(self) -> None:
        with self.assertRaises(PackageNotFoundError):
            parse_pm_path("\n", PKG)

    def test_pm_path_rejects_unexpected_path(self) -> None:
        with self.assertRaises(AdbError):
            parse_pm_path("package:/data/app/x/base.apk;id\n", PKG)

    def test_obb_listing_separates_stray_entries(self) -> None:
        self.assertEqual(parse_obb_listing("main.1.com.x.obb\n.nomedia\npatch.1.com.x.obb.tmp\n"),
                         (["main.1.com.x.obb"], [".nomedia", "patch.1.com.x.obb.tmp"]))

    def test_devices_requires_tab_separated_lines(self) -> None:
        with self.assertRaises(AdbError):
            parse_devices("List of devices attached\nadb server version (41) doesn't match this client\n")
        self.assertEqual(parse_devices("SER\tno permissions (user in plugdev)\n"),
                         [Device("SER", "no permissions (user in plugdev)")])

    def test_devices_rejects_control_characters(self) -> None:
        with self.assertRaises(AdbError):
            parse_devices("SER\x1b[2J\tdevice\n")

    def test_negative_retries_rejected(self) -> None:
        with self.assertRaises(AdbError):
            AdbConfig("adb", None, 5.0, 5.0, -1, 0.0)


class RetryTests(unittest.TestCase):
    def test_transient_failure_is_retried(self) -> None:
        script = {("devices",): [ok(stderr="error: device offline", code=1), ok("SER\tdevice\n")]}
        runner = FakeRunner(script, {})
        self.assertEqual(client(runner, retries=1).devices(), [Device("SER", "device")])
        self.assertEqual(len(runner.calls), 2)

    def test_retries_are_bounded(self) -> None:
        script = {("devices",): [ok(stderr="error: device offline", code=1)]}
        runner = FakeRunner(script, {})
        with self.assertRaises(AdbError):
            client(runner, retries=2).devices()
        self.assertEqual(len(runner.calls), 3)


class PullPackageTests(unittest.TestCase):
    def test_pulls_all_splits_and_writes_manifest(self) -> None:
        blobs = {BASE: b"base-apk", ABI: b"abi-apk"}
        runner = FakeRunner(device_script(), blobs)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pull"
            manifest = pull_package(client(runner, retries=0), PKG, out)
            self.assertEqual(manifest.device_serial, "SER123")
            self.assertEqual(manifest.version.version_name, "2.0.3")
            self.assertEqual([f.local_path for f in manifest.files],
                             ["apks/base.apk", "apks/split_config.arm64_v8a.apk"])
            self.assertEqual(manifest.files[0].sha256, hashlib.sha256(b"base-apk").hexdigest())
            written = json.loads((out / "manifest.json").read_text())
            self.assertEqual(written["package"], PKG)
            self.assertTrue(all(call[1:3] == ["-s", "SER123"] for call in runner.calls[1:]))

    def test_rejected_remote_path_leaves_no_output(self) -> None:
        script = device_script()
        script[("shell", "pm", "path", PKG)] = [ok(f"package:{BASE}\npackage:/data/app/x/../../etc/a.apk\n")]
        runner = FakeRunner(script, {BASE: b"base-apk"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AdbError):
                pull_package(client(runner, retries=0), PKG, Path(tmp) / "pull")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_refuses_non_empty_output(self) -> None:
        runner = FakeRunner(device_script(), {})
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "old.txt").write_text("x")
            with self.assertRaises(OutputDirectoryError):
                pull_package(client(runner, retries=0), PKG, Path(tmp))


if __name__ == "__main__":
    unittest.main()
