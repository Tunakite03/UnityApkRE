import functools
import hashlib
import json
import ssl
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

    def test_single_file_download_is_stored(self) -> None:
        (self.serve / "blob.bin").write_bytes(b"rawtool")
        url = self.url.rsplit("/", 1)[0] + "/blob.bin"
        record = fetch_archive(url, self.tmp / "blob", context=self.context)
        self.assertIn("blob", record.files)


if __name__ == "__main__":
    unittest.main()
