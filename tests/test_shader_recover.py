import json
import tempfile
import unittest
from pathlib import Path

from uare.shader_recover import recover_shaders, _reconstruct_shaderlab, _safe_stem


class SafeStemTests(unittest.TestCase):
    def test_basic_name(self):
        self.assertEqual(_safe_stem("Standard"), "Standard")

    def test_sanitizes_slashes(self):
        result = _safe_stem("UI/Default")
        self.assertNotIn("/", result)

    def test_empty_becomes_shader(self):
        self.assertEqual(_safe_stem(""), "shader")

    def test_truncation(self):
        long_name = "A" * 200
        self.assertEqual(len(_safe_stem(long_name)), 80)


class ReconstructTests(unittest.TestCase):
    def test_basic_reconstruction(self):
        data = {
            "m_ParsedForm": {
                "m_Name": "Custom/Test",
                "m_PropInfo": {"m_Props": [
                    {"m_Name": "_MainTex", "m_Description": "Main Texture", "m_Flags": 0,
                     "m_Type": 4, "m_DefValue": [0, 0, 0, 0]}
                ]},
                "m_SubShaders": [{
                    "m_Tags": {"tags": []},
                    "m_LOD": 200,
                    "m_Passes": [{"m_Name": "Forward", "m_Tags": {"tags": []}, "m_State": {}}]
                }],
                "m_FallbackName": "Diffuse",
                "m_CustomEditorName": "",
            }
        }
        source = _reconstruct_shaderlab(data)
        self.assertIn('Shader "Custom/Test"', source)
        self.assertIn("_MainTex", source)
        self.assertIn("LOD 200", source)
        self.assertIn('Fallback "Diffuse"', source)
        self.assertIn("CGPROGRAM", source)

    def test_empty_parsed_form(self):
        data = {"m_ParsedForm": {"m_Name": "Empty", "m_SubShaders": [], "m_PropInfo": {}}}
        source = _reconstruct_shaderlab(data)
        self.assertIn('Shader "Empty"', source)


class RecoverShadersTests(unittest.TestCase):
    def test_recover_original_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source" / "shaders"
            source.mkdir(parents=True)
            shader_data = {
                "m_Script": "Shader \"Custom/Original\" {\n  SubShader { Pass { } }\n}",
                "m_ParsedForm": {"m_Name": "Custom/Original", "m_SubShaders": []}
            }
            (source / "test.json").write_text(json.dumps(shader_data))
            out = Path(tmpdir) / "out"
            result = recover_shaders(source.parent, out)
            self.assertEqual(result.recovered_original, 1)
            self.assertEqual(result.recovered_parsed, 0)

    def test_recover_parsed_form(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source" / "shaders"
            source.mkdir(parents=True)
            shader_data = {
                "m_Script": "",
                "m_ParsedForm": {
                    "m_Name": "Custom/Parsed",
                    "m_SubShaders": [{"m_Passes": [{"m_Name": "", "m_State": {}}], "m_Tags": {"tags": []}}],
                    "m_PropInfo": {"m_Props": []}
                }
            }
            (source / "test.json").write_text(json.dumps(shader_data))
            out = Path(tmpdir) / "out"
            result = recover_shaders(source.parent, out)
            self.assertEqual(result.recovered_original, 0)
            self.assertEqual(result.recovered_parsed, 1)

    def test_skip_non_shader_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            source.mkdir(parents=True)
            (source / "not_shader.json").write_text(json.dumps({"foo": "bar"}))
            out = Path(tmpdir) / "out"
            result = recover_shaders(source, out)
            self.assertEqual(result.shaders_found, 0)

    def test_report_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source" / "shaders"
            source.mkdir(parents=True)
            shader_data = {
                "m_Script": "Shader \"Test\" { SubShader { Pass {} } }",
                "m_ParsedForm": {"m_Name": "Test", "m_SubShaders": []}
            }
            (source / "s.json").write_text(json.dumps(shader_data))
            out = Path(tmpdir) / "out"
            recover_shaders(source.parent, out)
            report = json.loads((out / "shader_report.json").read_text())
            self.assertEqual(report["total"], 1)


if __name__ == "__main__":
    unittest.main()
