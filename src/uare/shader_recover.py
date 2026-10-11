"""Recover shader source from Unity type-tree JSON exports.

Unity compiled shaders store their parsed form in ``m_ParsedForm`` and, when the source was
embedded, in ``m_Script``. The parsed form contains the SubShader/Pass structure, property
declarations, and references to compiled sub-programs (GLSL, Metal, HLSL, SPIR-V).

This module reads the shader JSON files produced by ``uare package`` (type-tree exports of
Shader objects) and recovers as much ShaderLab source as possible:

- If ``m_Script`` contains the original source, it is written as-is.
- Otherwise, the parsed form is reconstructed into ShaderLab syntax with placeholder
  sub-program bodies (the compiled bytecode cannot be decompiled here, but the structure,
  properties, passes, tags and render states are recovered).
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError
from uare.logs import event
from uare.outdir import staged_output

LOG = logging.getLogger("uare.shader_recover")


@dataclass(frozen=True)
class ShaderInfo:
    name: str
    source_file: str
    has_original: bool
    subshaders: int
    passes: int
    properties: int
    output: str | None
    error: str | None


@dataclass(frozen=True)
class ShaderResult:
    shaders_found: int
    recovered_original: int
    recovered_parsed: int
    failed: int
    shaders: tuple[ShaderInfo, ...]
    out_dir: str


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line.strip() else "" for line in text.splitlines())


def _render_properties(props: list[dict]) -> str:
    lines = ["Properties", "{"]
    for prop in props:
        name = prop.get("m_Name", "")
        desc = prop.get("m_Description", name)
        flags = prop.get("m_Flags", 0)
        tex_dim = prop.get("m_Type", 0)
        defval = prop.get("m_DefValue", [0, 0, 0, 0])
        if isinstance(defval, list) and len(defval) >= 4:
            default_str = f"({defval[0]}, {defval[1]}, {defval[2]}, {defval[3]})"
        else:
            default_str = str(defval)
        attrs = ""
        if flags & 1:
            attrs = "[HideInInspector] "
        if flags & 2:
            attrs += "[PerRendererData] "
        type_map = {0: "Float", 1: "Range(0, 1)", 2: "Vector", 3: "Color", 4: "2D", 5: "3D", 6: "Cube"}
        type_name = type_map.get(tex_dim, f"Float /* type {tex_dim} */")
        if tex_dim >= 4:
            lines.append(f'    {attrs}{name} ("{desc}", {type_name}) = "white" {{}}')
        else:
            lines.append(f'    {attrs}{name} ("{desc}", {type_name}) = {default_str}')
    lines.append("}")
    return "\n".join(lines)


def _render_tags(tags: dict) -> str:
    if not tags:
        return ""
    pairs = " ".join(f'"{k}"="{v}"' for k, v in tags.items())
    return f"Tags {{ {pairs} }}"


def _render_state(state: dict) -> list[str]:
    lines: list[str] = []
    name_map = {
        "m_Cull": "Cull", "m_ZTest": "ZTest", "m_ZWrite": "ZWrite",
        "m_Blend": "Blend", "m_AlphaToMask": "AlphaToMask", "m_Offset": "Offset",
        "m_ColorMask": "ColorMask", "m_Stencil": "Stencil",
    }
    for key, label in name_map.items():
        val = state.get(key)
        if val is not None and val != "" and val != 0:
            if isinstance(val, dict):
                lines.append(f"{label} {{ /* see type-tree */ }}")
            else:
                lines.append(f"{label} {val}")
    return lines


def _render_pass(p: dict, idx: int) -> str:
    lines: list[str] = []
    name = p.get("m_Name", "")
    if name:
        lines.append(f'Name "{name}"')
    tags = p.get("m_Tags", {}) or {}
    tag_map = {}
    for entry in (tags.get("tags", []) or []):
        tag_map[entry.get("first", "")] = entry.get("second", "")
    if tag_map:
        lines.append(_render_tags(tag_map))
    state = p.get("m_State", {}) or {}
    lines.extend(_render_state(state))
    programs = p.get("progVertex", {}) or p.get("m_Programs", []) or []
    if programs:
        lines.append("// Compiled sub-programs omitted (vertex/fragment bytecode)")
        lines.append("// Decompile with a platform-specific shader decompiler if needed")
    lines.append("CGPROGRAM")
    lines.append("// shader bytecode not recoverable from type-tree; stub only")
    lines.append(f"#pragma vertex vert")
    lines.append(f"#pragma fragment frag")
    lines.append("")
    lines.append("struct appdata { float4 vertex : POSITION; float2 uv : TEXCOORD0; };")
    lines.append("struct v2f { float4 vertex : SV_POSITION; float2 uv : TEXCOORD0; };")
    lines.append("")
    lines.append("v2f vert(appdata v) { v2f o; o.vertex = UnityObjectToClipPos(v.vertex); o.uv = v.uv; return o; }")
    lines.append("fixed4 frag(v2f i) : SV_Target { return fixed4(1, 0, 1, 1); /* placeholder */ }")
    lines.append("ENDCG")
    return "\n".join(lines)


def _render_subshader(ss: dict, idx: int) -> str:
    lines: list[str] = []
    tags = ss.get("m_Tags", {}) or {}
    tag_map = {}
    for entry in (tags.get("tags", []) or []):
        tag_map[entry.get("first", "")] = entry.get("second", "")
    if tag_map:
        lines.append(_render_tags(tag_map))
    lod = ss.get("m_LOD", 0)
    if lod:
        lines.append(f"LOD {lod}")
    passes = ss.get("m_Passes", []) or []
    for pi, p in enumerate(passes):
        pass_body = _render_pass(p, pi)
        lines.append(f"Pass\n{{\n{_indent(pass_body)}\n}}")
    return "\n".join(lines)


def _reconstruct_shaderlab(shader_data: dict) -> str:
    parsed = shader_data.get("m_ParsedForm", {}) or {}
    shader_name = parsed.get("m_Name", "Unknown/Recovered")
    lines: list[str] = [f'Shader "{shader_name}"', "{"]
    props = parsed.get("m_PropInfo", {}) or {}
    prop_list = props.get("m_Props", []) or []
    if prop_list:
        lines.append(_indent(_render_properties(prop_list)))
    subshaders = parsed.get("m_SubShaders", []) or []
    for si, ss in enumerate(subshaders):
        body = _render_subshader(ss, si)
        lines.append(f"\n    SubShader\n    {{\n{_indent(body, '        ')}\n    }}")
    fallback = parsed.get("m_FallbackName", "")
    if fallback:
        lines.append(f'    Fallback "{fallback}"')
    custom_editor = parsed.get("m_CustomEditorName", "")
    if custom_editor:
        lines.append(f'    CustomEditor "{custom_editor}"')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _safe_stem(name: str, max_len: int = 80) -> str:
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    return safe.strip("_. ")[:max_len] or "shader"


def recover_shaders(source_dir: Path, out_dir: Path) -> ShaderResult:
    """Scan for shader JSON files and recover ShaderLab source."""
    if not source_dir.is_dir():
        raise UareError(f"{source_dir} is not a directory")
    shader_jsons: list[Path] = []
    for candidate_dir in (source_dir / "assets" / "shaders", source_dir / "shaders", source_dir):
        if candidate_dir.is_dir():
            shader_jsons.extend(sorted(candidate_dir.rglob("*.json")))
    shader_jsons = list(dict.fromkeys(shader_jsons))

    results: list[ShaderInfo] = []
    recovered_orig = recovered_parsed = failed = 0

    with staged_output(out_dir) as staging:
        for json_path in shader_jsons:
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            script = data.get("m_Script")
            parsed = data.get("m_ParsedForm")
            if script is None and parsed is None:
                continue
            shader_name = ""
            if parsed and isinstance(parsed, dict):
                shader_name = parsed.get("m_Name", "")
            if not shader_name:
                shader_name = data.get("m_Name", json_path.stem)
            subshaders = len(parsed.get("m_SubShaders", [])) if parsed else 0
            passes = sum(len(ss.get("m_Passes", [])) for ss in (parsed.get("m_SubShaders", []) or [])) if parsed else 0
            props = len((parsed.get("m_PropInfo", {}) or {}).get("m_Props", [])) if parsed else 0
            safe = _safe_stem(shader_name)
            out_path = staging / f"{safe}.shader"
            counter = 0
            while out_path.exists():
                counter += 1
                out_path = staging / f"{safe}_{counter}.shader"
            try:
                if isinstance(script, str) and len(script.strip()) > 20:
                    out_path.write_text(script, encoding="utf-8")
                    recovered_orig += 1
                    results.append(ShaderInfo(shader_name, str(json_path), True, subshaders,
                                              passes, props, out_path.name, None))
                elif parsed:
                    source = _reconstruct_shaderlab(data)
                    out_path.write_text(source, encoding="utf-8")
                    recovered_parsed += 1
                    results.append(ShaderInfo(shader_name, str(json_path), False, subshaders,
                                              passes, props, out_path.name, None))
                else:
                    failed += 1
                    results.append(ShaderInfo(shader_name, str(json_path), False, 0, 0, 0,
                                              None, "no script or parsed form"))
            except Exception as exc:
                failed += 1
                results.append(ShaderInfo(shader_name, str(json_path), False, subshaders,
                                          passes, props, None, str(exc)))

        (staging / "shader_report.json").write_text(
            json.dumps({"total": len(results), "original": recovered_orig,
                        "reconstructed": recovered_parsed, "failed": failed,
                        "shaders": [{"name": s.name, "original": s.has_original,
                                     "output": s.output} for s in results]},
                       indent=2, ensure_ascii=False), encoding="utf-8")

    event(LOG, logging.INFO, "shaders_recovered", total=len(results), original=recovered_orig,
          reconstructed=recovered_parsed, failed=failed)
    return ShaderResult(len(results), recovered_orig, recovered_parsed, failed,
                        tuple(results), str(out_dir))


def summarize(result: ShaderResult) -> list[str]:
    return [
        f"shaders found     : {result.shaders_found}",
        f"original source   : {result.recovered_original}",
        f"reconstructed     : {result.recovered_parsed} (from parsed form, stub programs)",
        f"failed            : {result.failed}",
        f"output            : {result.out_dir}",
    ]
