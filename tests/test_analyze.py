import json
import os
import tempfile
import unittest
from pathlib import Path

from fixtures import elf64, il2cpp_metadata, serialized_v22, unityfs_bundle, write_zip
from uare.analyze import analyze, resolve_layout
from uare.cli import main
from uare.errors import OutputDirectoryError, UareError

GUID = "000385f0e1866fd4cb29245d14652f36"


def build_apk_set(root: Path) -> list[Path]:
    data = "assets/bin/Data/"
    ggm = serialized_v22("6000.2.15f1", 13, False, 19, 4096)
    level10 = serialized_v22("6000.2.15f1", 13, False, 383, 6000)
    base = write_zip(root / "base.apk", {
        "AndroidManifest.xml": b"axml",
        data + "globalgamemanagers": ggm,
        data + "level2": serialized_v22("6000.2.15f1", 13, False, 5, 4096),
        data + "level10.split0": level10[:3000],
        data + "boot.config": b"wait-for-native-debugger=0\nscripting-runtime-version=latest\n",
        data + "ScriptingAssemblies.json": json.dumps({"names": ["Assembly-CSharp.dll", "UnityEngine.dll"]}).encode(),
        data + "Managed/Metadata/global-metadata.dat": il2cpp_metadata(31, [b"Play"], b"Assembly-CSharp\x00LobbyUI\x00"),
    })
    assets = write_zip(root / "split_base_assets.apk", {
        data + "level10.split1": level10[3000:],
        data + GUID: serialized_v22("6000.2.15f1", 13, False, 2, 4096),
        data + GUID + ".resource": b"\x00" * 32,
        "assets/aa/Android/ui_assets_all.bundle": unityfs_bundle(flags=0x42),
    })
    abi = write_zip(root / "split_config.arm64_v8a.apk", {
        "lib/arm64-v8a/libil2cpp.so": elf64(os.urandom(1 << 20), ["il2cpp_init"], []),
        "lib/arm64-v8a/libbroken.so": b"not elf",
    })
    return [base, assets, abi]


class AnalyzeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cli_extract_then_analyze(self) -> None:
        archives = build_apk_set(self.tmp / "pull/apks")
        self.assertEqual(main(["extract", str(self.tmp / "pull"), "--out", str(self.tmp / "x")]), 0)
        extraction = json.loads((self.tmp / "x/extraction.json").read_text())
        self.assertEqual(sorted(Path(a["path"]).name for a in extraction["archives"]), sorted(p.name for p in archives))
        self.assertEqual(main(["analyze", str(self.tmp / "x"), "--out", str(self.tmp / "a")]), 0)

        report = json.loads((self.tmp / "a/report.json").read_text())
        inv = report["inventory"]
        self.assertEqual(report["scripting_backend"], "il2cpp")
        self.assertEqual(report["scripting_assemblies"], ["Assembly-CSharp.dll", "UnityEngine.dll"])
        self.assertEqual(report["boot_config"]["scripting-runtime-version"], "latest")
        self.assertEqual(inv["scene_files"], ["level2", "level10"])
        self.assertEqual(inv["unity_versions"], {"6000.2.15f1": 4})
        self.assertEqual(inv["target_platforms"], {"Android": 4})
        self.assertEqual(inv["guid_named_files"], 1)
        self.assertEqual(inv["kinds"]["resource"], 1)
        self.assertEqual(inv["parse_errors"], 0)
        self.assertEqual(report["il2cpp"]["status"], "ok")
        self.assertEqual(report["il2cpp"]["version"], 31)
        self.assertEqual((self.tmp / "a/il2cpp/identifiers.tsv").read_text(), "0\tAssembly-CSharp\n16\tLobbyUI\n")
        libs = {lib["name"]: lib for lib in report["native_libraries"]}
        self.assertEqual(libs["libil2cpp.so"]["report"]["packing_verdict"], "likely_packed_or_encrypted")
        self.assertIsNotNone(libs["libbroken.so"]["error"])
        files = (self.tmp / "a/files.jsonl").read_text().splitlines()
        self.assertEqual(len(files), inv["total_files"])

    def test_addressable_bundles_are_inventoried(self) -> None:
        build_apk_set(self.tmp / "pull/apks")
        main(["extract", str(self.tmp / "pull"), "--out", str(self.tmp / "x")])
        report = analyze(self.tmp / "x", self.tmp / "a")
        self.assertIsNotNone(report.assets_inventory)
        self.assertEqual(report.assets_inventory.bundle_compression, {"lz4": 1})
        record = json.loads((self.tmp / "a/assets_files.jsonl").read_text().splitlines()[0])
        self.assertEqual((record["path"], record["unity_version"]), ("aa/Android/ui_assets_all.bundle", "2021.3.45f1"))

    def test_raw_data_directory_is_accepted(self) -> None:
        data = self.tmp / "Data"
        data.mkdir()
        (data / "globalgamemanagers").write_bytes(serialized_v22("2022.3.1f1", 13, True, 3, 4096))
        report = analyze(data, self.tmp / "a")
        self.assertEqual(report.inventory.type_tree_files, 1)
        self.assertEqual(report.il2cpp.status, "absent")
        self.assertEqual(report.scripting_backend, "unknown")

    def test_encrypted_metadata_is_reported(self) -> None:
        data = self.tmp / "Data"
        (data / "Managed/Metadata").mkdir(parents=True)
        (data / "globalgamemanagers").write_bytes(serialized_v22("2022.3.1f1", 13, False, 3, 4096))
        (data / "Managed/Metadata/global-metadata.dat").write_bytes(os.urandom(4096))
        report = analyze(data, self.tmp / "a")
        self.assertEqual(report.il2cpp.status, "error")
        self.assertIn("encrypted", report.il2cpp.detail or "")

    def test_unknown_root_and_dirty_output(self) -> None:
        with self.assertRaises(UareError):
            resolve_layout(self.tmp)
        data = self.tmp / "Data"
        data.mkdir()
        (data / "globalgamemanagers").write_bytes(serialized_v22("2022.3.1f1", 13, False, 3, 4096))
        (self.tmp / "a").mkdir()
        (self.tmp / "a/old").write_text("x")
        with self.assertRaises(OutputDirectoryError):
            analyze(data, self.tmp / "a")

    def test_malformed_scripting_assemblies_is_a_domain_error(self) -> None:
        data = self.tmp / "Data"
        data.mkdir()
        (data / "globalgamemanagers").write_bytes(serialized_v22("2022.3.1f1", 13, False, 3, 4096))
        (data / "ScriptingAssemblies.json").write_text("[1, 2")
        with self.assertRaises(UareError):
            analyze(data, self.tmp / "a")
        self.assertFalse((self.tmp / "a").exists())

    def test_cli_returns_2_on_domain_error(self) -> None:
        self.assertEqual(main(["analyze", str(self.tmp), "--out", str(self.tmp / "a")]), 2)


@unittest.skipUnless(os.environ.get("UARE_REAL_DATA_DIR"), "set UARE_REAL_DATA_DIR to a real Unity Data directory")
class RealDataSmokeTest(unittest.TestCase):
    def test_real_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = analyze(Path(os.environ["UARE_REAL_DATA_DIR"]), Path(tmp) / "a")
        self.assertGreater(report.inventory.total_files, 0)
        self.assertEqual(report.inventory.parse_errors, 0)


if __name__ == "__main__":
    unittest.main()
