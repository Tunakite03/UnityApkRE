import os
import stat
import tempfile
import unittest
from pathlib import Path

from uare.assetripper import AssetRipperError, android_layout, read_log, resolve_assetripper


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class AssetRipperTests(unittest.TestCase):
    def test_resolution_order_is_explicit_env_vendor_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vendored = _executable(root / "vendor" / "AssetRipper.GUI.Free")
            explicit = _executable(root / "explicit")
            self.assertEqual(resolve_assetripper(str(explicit), {}, root / "vendor", lambda n: None).source, "--assetripper")
            self.assertEqual(resolve_assetripper(None, {"UARE_ASSETRIPPER": str(explicit)}, root / "vendor",
                                                 lambda n: None).source, "UARE_ASSETRIPPER")
            self.assertEqual(resolve_assetripper(None, {}, root / "vendor", lambda n: None).path, str(vendored))
            with self.assertRaises(AssetRipperError):
                resolve_assetripper(str(root / "missing"), {}, root / "vendor", lambda n: None)
            with self.assertRaises(AssetRipperError):
                resolve_assetripper(None, {}, root / "empty", lambda n: None)

    def test_extracted_tree_is_presented_in_the_android_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "extracted"
            (root / "Data" / "Managed" / "Metadata").mkdir(parents=True)
            (root / "Data" / "globalgamemanagers").write_bytes(b"\0" * 64)
            (root / "lib" / "arm64-v8a").mkdir(parents=True)
            (root / "assets" / "aa").mkdir(parents=True)
            target = android_layout(root, Path(tmp) / "input")
            self.assertTrue((target / "assets" / "bin" / "Data").is_symlink())
            self.assertEqual(os.path.realpath(target / "lib"), os.path.realpath(root / "lib"))
            self.assertTrue((target / "assets" / "aa").is_symlink())

    def test_log_reports_the_unknown_fallback_after_an_il2cpp_failure(self) -> None:
        text = ("Import : Files use the 'IL2Cpp' scripting backend.\n"
                "Import [Error] : Could not initialize assembly manager. Switching to the 'Unknown' scripting backend.\n"
                "Export : Finished post-export\n")
        backend, fields, errors = read_log(text)
        self.assertEqual((backend, fields, len(errors)), ("Unknown", False, 1))
        self.assertEqual(read_log("Import : Files use the 'IL2Cpp' scripting backend.\n")[:2], ("IL2Cpp", True))


if __name__ == "__main__":
    unittest.main()
