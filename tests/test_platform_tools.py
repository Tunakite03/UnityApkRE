import hashlib
import io
import json
import os
import ssl
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import urllib.request
from email.message import Message

from uare.platform_tools import (
    PlatformToolsError,
    _HttpsOnlyRedirect,
    _total_size,
    download,
    fetch_platform,
    parse_revision,
    path_lookup,
    resolve_adb,
    unpack,
    vendored_adb,
)


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value


class ProtocolGuardTests(unittest.TestCase):
    def test_https_to_http_redirect_is_refused(self) -> None:
        request = urllib.request.Request("https://dl.google.com/x.zip")
        with self.assertRaises(PlatformToolsError):
            _HttpsOnlyRedirect().redirect_request(request, None, 302, "Found", Message(), "http://evil.test/x.zip")

    def test_malformed_content_range_is_a_domain_error(self) -> None:
        with self.assertRaisesRegex(PlatformToolsError, "Content-Range"):
            _total_size(FakeResponse(206, {"Content-Range": "bytes */oops"}), 10)  # type: ignore[arg-type]
        with self.assertRaisesRegex(PlatformToolsError, "resumed at byte 5"):
            _total_size(FakeResponse(206, {"Content-Range": "bytes 5-9/10"}), 3)  # type: ignore[arg-type]
        with self.assertRaisesRegex(PlatformToolsError, "Content-Length"):
            _total_size(FakeResponse(200, {"Content-Length": "-1"}), 0)  # type: ignore[arg-type]


def platform_tools_zip(platform: str) -> bytes:
    names = ["adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"] if platform == "windows" else ["adb"]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.writestr(f"platform-tools/{name}", f"binary {name}".encode())
        zf.writestr("platform-tools/NOTICE.txt", "notices")
        zf.writestr("platform-tools/source.properties", "Pkg.UserSrc=false\nPkg.Revision=37.0.1\n")
        zf.writestr("platform-tools/fastboot", "not vendored")
    return buffer.getvalue()


class TruncatingServer:
    """Serves one payload; the first full GET is cut off halfway, Range requests are honoured."""

    def __init__(self, payload: bytes, honour_range: bool) -> None:
        self.payload = payload
        self.requests: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:
                rng = self.headers.get("Range")
                outer.requests.append(rng)
                if rng and honour_range:
                    start = int(rng.split("=")[1].rstrip("-"))
                    body = outer.payload[start:]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(outer.payload) - 1}/{len(outer.payload)}")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(outer.payload)))
                self.end_headers()
                cut = len(outer.payload) // 2 if len(outer.requests) == 1 else len(outer.payload)
                self.wfile.write(outer.payload[:cut])
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/platform-tools.zip"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "TruncatingServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


class LocalNetworkCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = {k: os.environ.get(k) for k in ("no_proxy", "NO_PROXY")}
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

    def tearDown(self) -> None:
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


class ResolveAdbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.vendor = self.tmp / "vendor"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def executable(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def vendored(self, platform: str) -> Path:
        adb = self.executable(vendored_adb(self.vendor, platform))
        record = {"files": {adb.name: hashlib.sha256(adb.read_bytes()).hexdigest()}}
        (adb.parent / "FETCHED.json").write_text(json.dumps(record))
        return adb

    def test_tampered_vendored_adb_is_refused(self) -> None:
        adb = self.vendored("darwin")
        adb.write_text("#!/bin/sh\necho evil\n")
        with self.assertRaisesRegex(PlatformToolsError, "modified"):
            resolve_adb(None, {}, self.vendor, "darwin", lambda name: None)

    def test_vendored_adb_without_record_is_refused(self) -> None:
        self.executable(vendored_adb(self.vendor, "darwin"))
        with self.assertRaisesRegex(PlatformToolsError, "FETCHED.json"):
            resolve_adb(None, {}, self.vendor, "darwin", lambda name: None)

    def test_path_lookup_skips_relative_entries(self) -> None:
        self.executable(self.tmp / "abs/adb")
        cwd = Path.cwd()
        os.chdir(self.tmp)
        try:
            self.executable(self.tmp / "adb")
            self.assertIsNone(path_lookup("adb", os.pathsep.join(["", ".", "abs"])))
            self.assertEqual(path_lookup("adb", os.pathsep.join([".", str(self.tmp / "abs")])),
                             str(self.tmp / "abs/adb"))
        finally:
            os.chdir(cwd)

    def test_order_explicit_env_vendored_path(self) -> None:
        explicit = self.executable(self.tmp / "explicit/adb")
        env_adb = self.executable(self.tmp / "env/adb")
        bundled = self.vendored("darwin")
        which = lambda name: "/usr/bin/adb"  # noqa: E731
        env = {"UARE_ADB": str(env_adb)}
        self.assertEqual(resolve_adb(str(explicit), env, self.vendor, "darwin", which).source, "--adb")
        self.assertEqual(resolve_adb(None, env, self.vendor, "darwin", which).source, "UARE_ADB")
        resolved = resolve_adb(None, {}, self.vendor, "darwin", which)
        self.assertEqual((resolved.source, resolved.path), ("vendored", str(bundled)))
        self.assertEqual(resolve_adb(None, {}, self.tmp / "none", "darwin", which).source, "PATH")

    def test_invalid_explicit_choice_is_an_error_not_a_fallback(self) -> None:
        which = lambda name: "/usr/bin/adb"  # noqa: E731
        with self.assertRaises(PlatformToolsError):
            resolve_adb(str(self.tmp / "missing"), {}, self.vendor, "darwin", which)
        with self.assertRaises(PlatformToolsError):
            resolve_adb(None, {"UARE_ADB": str(self.tmp / "missing")}, self.vendor, "darwin", which)

    def test_nothing_found(self) -> None:
        with self.assertRaisesRegex(PlatformToolsError, "fetch-adb"):
            resolve_adb(None, {}, self.vendor, "linux", lambda name: None)

    def test_windows_binary_name(self) -> None:
        self.assertEqual(vendored_adb(self.vendor, "windows").name, "adb.exe")
        with self.assertRaises(PlatformToolsError):
            vendored_adb(self.vendor, "plan9")


class UnpackTests(unittest.TestCase):
    def test_unpacks_only_needed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "pt.zip"
            archive.write_bytes(platform_tools_zip("windows"))
            dest = Path(tmp) / "out"
            dest.mkdir()
            hashes = unpack(archive, "windows", dest)
            self.assertEqual(sorted(p.name for p in dest.iterdir()),
                             ["AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt", "adb.exe", "source.properties"])
            self.assertEqual(hashes["adb.exe"], hashlib.sha256(b"binary adb.exe").hexdigest())
            self.assertTrue(os.access(dest / "adb.exe", os.X_OK))

    def test_missing_member_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "pt.zip"
            archive.write_bytes(platform_tools_zip("linux"))
            dest = Path(tmp) / "out"
            dest.mkdir()
            with self.assertRaises(PlatformToolsError):
                unpack(archive, "windows", dest)

    def test_revision(self) -> None:
        self.assertEqual(parse_revision("Pkg.UserSrc=false\nPkg.Revision=37.0.1\n"), "37.0.1")
        with self.assertRaises(PlatformToolsError):
            parse_revision("Pkg.UserSrc=false\n")


class DownloadTests(LocalNetworkCase):
    def test_resumes_after_truncated_connection(self) -> None:
        payload = os.urandom(300_000)
        with TruncatingServer(payload, honour_range=True) as server:
            download(server.url, self.tmp / "file", ssl.create_default_context(), retries=2)
        self.assertEqual((self.tmp / "file").read_bytes(), payload)
        self.assertEqual(server.requests, [None, "bytes=150000-"])

    def test_restarts_when_range_is_ignored(self) -> None:
        payload = os.urandom(200_000)
        with TruncatingServer(payload, honour_range=False) as server:
            download(server.url, self.tmp / "file", ssl.create_default_context(), retries=2)
        self.assertEqual((self.tmp / "file").read_bytes(), payload)

    def test_fetch_platform_end_to_end(self) -> None:
        payload = platform_tools_zip("linux")
        vendor = self.tmp / "vendor"
        with TruncatingServer(payload, honour_range=True) as server:
            record = fetch_platform("linux", server.url, vendor, hashlib.sha256(payload).hexdigest(),
                                    ssl.create_default_context())
        self.assertEqual(record.revision, "37.0.1")
        self.assertEqual(sorted(p.name for p in (vendor / "linux").iterdir()),
                         ["FETCHED.json", "NOTICE.txt", "adb", "source.properties"])
        self.assertEqual(json.loads((vendor / "linux/FETCHED.json").read_text())["archive_sha256"],
                         hashlib.sha256(payload).hexdigest())

    def test_fetch_rejects_wrong_sha_and_leaves_nothing(self) -> None:
        payload = platform_tools_zip("linux")
        vendor = self.tmp / "vendor"
        with TruncatingServer(payload, honour_range=True) as server:
            with self.assertRaisesRegex(PlatformToolsError, "sha256"):
                fetch_platform("linux", server.url, vendor, "0" * 64, ssl.create_default_context())
        self.assertEqual(list(vendor.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
