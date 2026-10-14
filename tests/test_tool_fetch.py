import functools
import hashlib
import io
import json
import ssl
import tarfile
import tempfile
import threading
import unittest
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from uare.errors import UareError
from uare.tool_fetch import fetch_archive


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence test output
        pass


class ToolFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.serve = self.tmp / "serve"
        self.serve.mkdir()
        self._zip = self.serve / "tool.zip"
        with zipfile.ZipFile(self._zip, "w") as zf:
            zf.writestr("bin/tool", "binary")
            zf.writestr("README.txt", "hello")
        handler = functools.partial(_Quiet, directory=str(self.serve))
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/tool.zip"
        self.context = ssl.create_default_context()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def test_downloads_and_unpacks_zip(self) -> None:
        record = fetch_archive(self.url, self.tmp / "out", context=self.context)
        self.assertEqual(record.files["bin/tool"], hashlib.sha256(b"binary").hexdigest())
        self.assertEqual((self.tmp / "out/bin/tool").read_text(), "binary")
        self.assertTrue((self.tmp / "out/FETCHED.json").is_file())
        data = json.loads((self.tmp / "out/FETCHED.json").read_text())
        self.assertEqual(data["url"], self.url)

    def test_sha256_mismatch_is_rejected(self) -> None:
        with self.assertRaises(UareError):
            fetch_archive(self.url, self.tmp / "out", expected_sha256="00" * 32, context=self.context)
        self.assertFalse((self.tmp / "out").exists())

    def test_traversal_member_is_rejected(self) -> None:
        evil = self.serve / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escape", "x")
        url = self.url.rsplit("/", 1)[0] + "/evil.zip"
        with self.assertRaises(UareError):
            fetch_archive(url, self.tmp / "out", context=self.context)
        self.assertFalse((self.tmp / "escape").exists())

    def _tar(self, name: str, entries: list[tarfile.TarInfo], payloads: dict[str, bytes]) -> str:
        with tarfile.open(self.serve / name, "w:xz") as tf:
            for info in entries:
                tf.addfile(info, io.BytesIO(payloads[info.name]) if info.isfile() else None)
        return self.url.rsplit("/", 1)[0] + "/" + name

    def test_tar_xz_keeps_executable_mode(self) -> None:
        info = tarfile.TarInfo("AssetRipper/AssetRipper.GUI.Free")
        info.size, info.mode = 6, 0o755
        url = self._tar("tool.tar.xz", [info], {info.name: b"binary"})
        record = fetch_archive(url, self.tmp / "out", context=self.context)
        target = self.tmp / "out/AssetRipper/AssetRipper.GUI.Free"
        self.assertEqual(record.files[info.name], hashlib.sha256(b"binary").hexdigest())
        self.assertTrue(target.stat().st_mode & 0o100)

    def test_tar_link_out_of_the_destination_is_rejected(self) -> None:
        link = tarfile.TarInfo("escape")
        link.type, link.linkname = tarfile.SYMTYPE, "../../outside"
        url = self._tar("evil.tar.xz", [link], {})
        with self.assertRaises(UareError):
            fetch_archive(url, self.tmp / "out", context=self.context)
        self.assertFalse((self.tmp / "out").exists())

    def test_single_file_download_is_stored(self) -> None:
        (self.serve / "blob.bin").write_bytes(b"rawtool")
        url = self.url.rsplit("/", 1)[0] + "/blob.bin"
        record = fetch_archive(url, self.tmp / "blob", context=self.context)
        self.assertIn("blob", record.files)


if __name__ == "__main__":
    unittest.main()
