import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from fixtures import il2cpp_metadata, serialized_v22
from uare.cli import main
from uare.errors import UareError
from uare.search import format_hits, search_rows

ROOT = Path(__file__).resolve().parents[1]


def run_cli(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


class SearchRowsTests(unittest.TestCase):
    ROWS = [(str(i), name) for i, name in enumerate(
        ["Game", "ShopPage", "OnBuy", "price", "LobbyUI", "Refresh", "ShopSlot", "Bind"])]

    def test_context_merges_overlapping_windows(self) -> None:
        hits = search_rows(self.ROWS, "^Shop", context=1, limit=10)
        self.assertEqual([h.value for h in hits], ["Game", "ShopPage", "OnBuy", "Refresh", "ShopSlot", "Bind"])
        self.assertEqual(format_hits(hits)[3], "--")
        self.assertTrue(format_hits(hits)[1].startswith("> 1\tShopPage"))

    def test_limit_counts_matches(self) -> None:
        self.assertEqual(len(search_rows(self.ROWS, "Shop", context=0, limit=1)), 1)

    def test_game_text_cannot_forge_output_lines(self) -> None:
        rows = [("0", "Buy\n> 99\tFakeHit x"), ("1", "A" * 400)]
        lines = format_hits(search_rows(rows, ".", context=0, limit=5))
        self.assertEqual(len(lines), 2)
        self.assertIn("Buy\\n> 99\\tFakeHit\\u2028x", lines[0])
        self.assertTrue(lines[1].endswith("...(+100)"))

    def test_bad_regex_and_arguments(self) -> None:
        with self.assertRaises(UareError):
            search_rows(self.ROWS, "(", context=0, limit=1)
        with self.assertRaises(UareError):
            search_rows(self.ROWS, "x", context=-1, limit=1)


class AnalysisCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        data = self.tmp / "Data"
        (data / "Managed/Metadata").mkdir(parents=True)
        (data / "globalgamemanagers").write_bytes(serialized_v22("6000.2.15f1", 13, False, 19, 4096))
        heap = b"Game\x00MainLobbyPage\x00OnClickStart\x00m_energy\x00LobbyUI\x00"
        (data / "Managed/Metadata/global-metadata.dat").write_bytes(
            il2cpp_metadata(31, [b"Start Game", b"Energy: {0}"], heap))
        self.analysis = self.tmp / "analysis"
        code, _ = run_cli(["analyze", str(data), "--out", str(self.analysis)])
        self.assertEqual(code, 0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_summary_reprints_report(self) -> None:
        code, out = run_cli(["summary", str(self.analysis)])
        self.assertEqual(code, 0)
        self.assertIn("il2cpp metadata   : v31, 5 identifiers, 2 literals", out)

    def test_find_identifiers_with_context(self) -> None:
        code, out = run_cli(["find", str(self.analysis), "Lobby", "--context", "1"])
        self.assertEqual(code, 0)
        self.assertIn(">", out.splitlines()[1])
        self.assertIn("MainLobbyPage", out)
        self.assertIn("OnClickStart", out)

    def test_find_literals_and_no_match(self) -> None:
        self.assertEqual(run_cli(["find", str(self.analysis), "(?i)energy", "--literals"])[0], 0)
        self.assertEqual(run_cli(["find", str(self.analysis), "Nope"])[0], 1)

    def test_summary_of_non_analysis_dir_fails(self) -> None:
        self.assertEqual(run_cli(["summary", str(self.tmp)])[0], 2)


class WrapperTests(unittest.TestCase):
    def test_wrapper_runs_from_any_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([str(ROOT / "scripts/uare"), "--help"], cwd=tmp, capture_output=True,
                                    text=True, timeout=60, check=False,
                                    env={**os.environ, "PYTHONPATH": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fetch-adb", result.stdout)

    def test_plugin_manifests_are_consistent(self) -> None:
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])
        from uare import __version__
        self.assertEqual(plugin["version"], __version__)
        for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
            head = skill.read_text().split("---")[1]
            self.assertIn(f"name: {skill.parent.name}\n", head, skill)
            self.assertIn("description: ", head, skill)


@unittest.skipUnless((ROOT / "vendor/platform-tools").is_dir(), "vendored platform-tools not present")
class VendoredAdbTests(unittest.TestCase):
    def test_vendored_files_match_fetch_record(self) -> None:
        import hashlib
        for platform_dir in sorted((ROOT / "vendor/platform-tools").iterdir()):
            record = json.loads((platform_dir / "FETCHED.json").read_text())
            for name, digest in record["files"].items():
                actual = hashlib.sha256((platform_dir / name).read_bytes()).hexdigest()
                self.assertEqual(actual, digest, f"{platform_dir.name}/{name}")


if __name__ == "__main__":
    unittest.main()
