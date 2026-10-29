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

    def test_type_tree_shapes_from_unitypy(self):
        """Field names and enum values as UnityPy's read_typetree() returns them for a Unity 6 shader."""
        def prop(name, kind, values, tex_dim=1, default_name="", flags=0, attrs=()):
            row = {"m_Name": name, "m_Description": name.strip("_"), "m_Attributes": list(attrs),
                   "m_Type": kind, "m_Flags": flags, "m_DefTexture": {"m_DefaultName": default_name, "m_TexDim": tex_dim}}
            row.update({f"m_DefValue[{i}]": v for i, v in enumerate(values)})
            return row

        def value(val, name="<noninit>"):
            return {"val": val, "name": name}

        blend = {"srcBlend": value(5.0), "destBlend": value(10.0), "srcBlendAlpha": value(5.0),
                 "destBlendAlpha": value(10.0), "blendOp": value(0.0), "blendOpAlpha": value(0.0),
                 "colMask": value(0.0, "_ColorMask")}
        off = {"srcBlend": value(1.0), "destBlend": value(0.0), "srcBlendAlpha": value(1.0),
               "destBlendAlpha": value(0.0), "blendOp": value(0.0), "blendOpAlpha": value(0.0), "colMask": value(15.0)}
        stencil = {"pass": value(0.0, "_StencilOp"), "fail": value(0.0), "zFail": value(0.0),
                   "comp": value(0.0, "_StencilComp")}
        tags = {"tags": [["Queue", "Transparent"], ["RenderType", "Transparent"]]}
        state = {"m_Name": "Default", "rtBlend0": blend, **{f"rtBlend{i}": off for i in range(1, 8)},
                 "rtSeparateBlend": False, "zTest": value(0.0, "unity_GUIZTestMode"), "zWrite": value(0.0),
                 "culling": value(0.0), "offsetFactor": value(0.0), "offsetUnits": value(0.0),
                 "alphaToMask": value(0.0), "stencilOp": stencil, "stencilOpFront": stencil, "stencilOpBack": stencil,
                 "stencilRef": value(0.0, "_Stencil"), "stencilReadMask": value(255.0, "_StencilReadMask"),
                 "stencilWriteMask": value(255.0), "m_Tags": {"tags": [["Queue", "Transparent"], ["LightMode", "Always"]]},
                 "m_LOD": 0}
        data = {"m_ParsedForm": {
            "m_Name": "UI/Recovered",
            "m_PropInfo": {"m_Props": [
                prop("_MainTex", 4, (0, 0, 0, 0), tex_dim=2, default_name="white", flags=2),
                prop("_Color", 0, (1, 0.5, 1, 1)),
                prop("_Glow", 3, (0.25, 0, 2, 0)),
                prop("_Steps", 5, (3, 0, 0, 0)),
                prop("_Clip", 2, (0, 0, 0, 0), attrs=("Toggle(UNITY_UI_ALPHACLIP)",)),
            ]},
            "m_SubShaders": [{"m_Tags": tags, "m_LOD": 0, "m_Passes": [
                {"m_Type": 0, "m_Name": "", "m_Tags": {"tags": []}, "m_State": state},
                {"m_Type": 2, "m_TextureName": "_GrabTex", "m_Tags": {"tags": []}, "m_State": {}},
            ]}],
        }}
        source = _reconstruct_shaderlab(data)
        for line in ('[PerRendererData] _MainTex ("MainTex", 2D) = "white" {}',
                     '_Color ("Color", Color) = (1, 0.5, 1, 1)',
                     '_Glow ("Glow", Range(0, 2)) = 0.25',
                     '_Steps ("Steps", Integer) = 3',
                     '[Toggle(UNITY_UI_ALPHACLIP)] _Clip ("Clip", Float) = 0',
                     'Tags { "Queue"="Transparent" "RenderType"="Transparent" }',
                     'Name "Default"', 'Tags { "LightMode"="Always" }',
                     "Cull Off", "ZWrite Off", "ZTest [unity_GUIZTestMode]",
                     "Blend SrcAlpha OneMinusSrcAlpha", "ColorMask [_ColorMask]",
                     "Ref [_Stencil]", "ReadMask [_StencilReadMask]", "Comp [_StencilComp]", "Pass [_StencilOp]",
                     'GrabPass { "_GrabTex" }', '#include "UnityCG.cginc"'):
            self.assertIn(line, source)
        self.assertNotIn("WriteMask", source)  # 255 is the default and not bound to a property
        self.assertNotIn("CompFront", source)  # identical to the two-sided operation

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

    def test_shared_shaders_are_the_only_search_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            package = Path(tmpdir) / "extracted-assets"
            shader_data = {
                "m_Script": "Shader \"Shared/Only\" { SubShader { Pass {} } }",
                "m_ParsedForm": {"m_Name": "Shared/Only", "m_SubShaders": []}
            }
            (package / "shared" / "shaders").mkdir(parents=True)
            (package / "shared" / "shaders" / "only.json").write_text(json.dumps(shader_data))
            (package / "scenes" / "Login").mkdir(parents=True)
            (package / "scenes" / "Login" / "scene.json").write_text(json.dumps({"roots": []}))
            out = Path(tmpdir) / "out"
            result = recover_shaders(package, out)
            self.assertEqual(result.shaders_found, 1)

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
