import json
import tempfile
import unittest
from pathlib import Path

from uare.manifest import MANIFEST_NAME, RunLog, build_manifest, write_manifest


def _write(root: Path, rel: str, data: bytes | str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        target.write_text(data, encoding="utf-8")
    else:
        target.write_bytes(data)


def _workdir(root: Path, skipped: int = 0, errors: int = 0) -> None:
    _write(root, "pull/manifest.json", json.dumps({
        "package": "com.example.game", "device_serial": "ABC", "version": {"version_name": "1", "version_code": "1"},
        "files": [{"kind": "apk", "remote_path": "/x/base.apk", "local_path": "apks/base.apk",
                   "size": 4, "sha256": "f" * 64}]}))
    _write(root, "pull/apks/base.apk", b"apk!")
    _write(root, "analysis/report.json", json.dumps({
        "scripting_backend": "il2cpp",
        "inventory": {"unity_versions": {"2022.3.62f2c1": 3}, "target_platforms": {"Android": 3},
                      "scene_files": ["level0"]},
        "il2cpp": {"status": "ok", "version": 31, "identifiers": 10, "string_literals": 5},
        "native_libraries": [{"abi": "arm64-v8a", "name": "libil2cpp.so"}],
    }))
    _write(root, "extracted-assets/report.json", json.dumps({
        "files": 2, "objects": 6, "exported": 3, "in_hierarchy": 1, "skipped": skipped, "errors": errors,
        "categories": {"textures": 1}, "scenes": ["level0"]}))
    _write(root, "extracted-assets/index.html", "<html></html>")
    _write(root, "extracted-assets/scripts/hotdll/GameCore.dll", b"\x00\x01\x02")
    _write(root, "extracted-assets/scripts/hotdll/Real.dll", b"MZ\x90\x00")
    catalog = [
        {"path_id": 1, "class_name": "TextAsset", "name": "GameCore.dll", "category": "scripts",
         "source": "a", "output": "scripts/hotdll/GameCore.dll", "bytes": 3, "note": None},
        {"path_id": 2, "class_name": "TextAsset", "name": "Real.dll", "category": "scripts",
         "source": "a", "output": "scripts/hotdll/Real.dll", "bytes": 4, "note": None},
        {"path_id": 3, "class_name": "TextAsset", "name": "Arrow.bank", "category": "audio",
         "source": "a", "output": "assets/audio/x/Arrow.bank", "bytes": 9, "note": "2 subsounds decoded into Arrow/"},
        {"path_id": 4, "class_name": "TextAsset", "name": "Master.bank", "category": "audio",
         "source": "a", "output": "assets/audio/x/Master.bank", "bytes": 9,
         "note": "FMOD bank without FSB5 sample data (event metadata only)"},
        {"path_id": 5, "class_name": "MonoBehaviour", "name": "", "category": "unclassified", "source": "a",
         "output": None, "bytes": 0, "note": "no type tree (ValueError: Expected to read 68 bytes)"},
        {"path_id": 6, "class_name": "MonoBehaviour", "name": "", "category": "unclassified", "source": "a",
         "output": None, "bytes": 0, "note": "no type tree (ValueError: Expected to read 280 bytes)"},
    ]
    _write(root, "extracted-assets/catalog.json", json.dumps(catalog))


def _complete_run() -> RunLog:
    run = RunLog()
    for name in ("pull", "extract", "analyze", "package"):
        run.stage(name, lambda: None)
    return run


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_complete_run_with_skips_is_partial_and_says_why(self) -> None:
        _workdir(self.root, skipped=2)
        manifest = build_manifest(self.root, _complete_run())
        self.assertEqual(manifest["status"], "partial")
        self.assertIn("2 object(s) produced no output", manifest["status_reason"])

    def test_clean_run_is_complete(self) -> None:
        _workdir(self.root, skipped=0)
        manifest = build_manifest(self.root, _complete_run())
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual([s["status"] for s in manifest["stages"]], ["ok", "ok", "ok", "ok"])

    def test_failed_stage_is_recorded_and_later_stages_are_not_run(self) -> None:
        _workdir(self.root)
        run = RunLog()
        run.stage("pull", lambda: None)
        with self.assertRaises(RuntimeError):
            run.stage("extract", lambda: (_ for _ in ()).throw(RuntimeError("zip is corrupt")))
        manifest = build_manifest(self.root, run)
        self.assertEqual(manifest["status"], "failed")
        by_name = {s["name"]: s for s in manifest["stages"]}
        self.assertEqual(by_name["extract"]["status"], "failed")
        self.assertIn("zip is corrupt", by_name["extract"]["error"])
        self.assertEqual(by_name["analyze"]["status"], "not_run")
        self.assertEqual(by_name["package"]["status"], "not_run")

    def test_inputs_and_outputs_carry_hashes_and_existence(self) -> None:
        _workdir(self.root)
        manifest = build_manifest(self.root, _complete_run())
        self.assertEqual(manifest["inputs"][0]["path"], "pull/apks/base.apk")
        self.assertEqual(manifest["inputs"][0]["bytes"], 4)
        roles = {o["role"]: o for o in manifest["outputs"]}
        self.assertTrue(roles["asset_catalog"]["exists"])
        self.assertEqual(len(roles["asset_catalog"]["sha256"]), 64)
        self.assertFalse(roles["extraction"]["exists"])
        self.assertIsNone(roles["extraction"]["sha256"])

    def test_hot_update_assemblies_are_classified_by_header(self) -> None:
        _workdir(self.root)
        manifest = build_manifest(self.root, _complete_run())
        formats = {a["path"].rsplit("/", 1)[-1]: a["format"] for a in manifest["coverage"]["hot_update_assemblies"]}
        self.assertEqual(formats, {"GameCore.dll": "not-pe", "Real.dll": "pe"})
        self.assertIn("1 hot-update assembly file(s) are not plain .NET assemblies",
                      " ".join(manifest["limitations"]))

    def test_unexported_reasons_are_grouped_with_numbers_normalised(self) -> None:
        _workdir(self.root, skipped=2)
        manifest = build_manifest(self.root, _complete_run())
        by_reason = manifest["coverage"]["unexported_objects"]["by_reason"]
        self.assertEqual(by_reason, {"no type tree (ValueError: Expected to read N bytes)": 2})
        self.assertEqual(manifest["coverage"]["unexported_objects"]["total"], 2)

    def test_fmod_banks_split_into_decoded_and_metadata_only(self) -> None:
        _workdir(self.root)
        manifest = build_manifest(self.root, _complete_run())
        self.assertEqual(manifest["coverage"]["fmod_banks"], {"with_samples": 1, "metadata_only": 1, "total": 2})

    def test_scope_lists_never_claim_decryption_or_decompilation(self) -> None:
        _workdir(self.root)
        manifest = build_manifest(self.root, _complete_run())
        self.assertEqual(manifest["scope"], {"decrypted": [], "decompiled": []})

    def test_write_manifest_creates_the_file_in_the_workdir_root(self) -> None:
        _workdir(self.root)
        written = write_manifest(self.root, _complete_run())
        on_disk = json.loads((self.root / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["status"], written["status"])
        self.assertEqual(on_disk["schema"], "uare.run-manifest")
        self.assertEqual(on_disk["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
