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


# Enum values as Unity serializes them in SerializedShader (UnityEngine.Rendering / ShaderPropertyType).
_PROP_COLOR, _PROP_VECTOR, _PROP_FLOAT, _PROP_RANGE, _PROP_TEXTURE, _PROP_INT = range(6)
_TEX_DIMS = {2: "2D", 3: "3D", 4: "Cube", 5: "2DArray", 6: "CubeArray"}
_PROP_FLAGS = ((1, "HideInInspector"), (2, "PerRendererData"), (4, "NoScaleOffset"), (8, "Normal"),
               (16, "HDR"), (32, "Gamma"), (128, "MainTexture"), (256, "MainColor"))
_CULL = {0: "Off", 1: "Front", 2: "Back"}
# CompareFunction 0 is "Disabled", which behaves as Always.
_COMPARE = {0: "Always", 1: "Never", 2: "Less", 3: "Equal", 4: "LEqual", 5: "Greater", 6: "NotEqual",
            7: "GEqual", 8: "Always"}
_BLEND = {0: "Zero", 1: "One", 2: "DstColor", 3: "SrcColor", 4: "OneMinusDstColor", 5: "SrcAlpha",
          6: "OneMinusSrcColor", 7: "DstAlpha", 8: "OneMinusDstAlpha", 9: "SrcAlphaSaturate", 10: "OneMinusSrcAlpha"}
_BLEND_OP = {0: "Add", 1: "Sub", 2: "RevSub", 3: "Min", 4: "Max"}
_STENCIL_OP = {0: "Keep", 1: "Zero", 2: "Replace", 3: "IncrSat", 4: "DecrSat", 5: "Invert", 6: "IncrWrap",
               7: "DecrWrap"}
_PASS_NORMAL, _PASS_USE, _PASS_GRAB = 0, 1, 2
_UNBOUND = ("", "<noninit>")


def _text(value: object) -> str:
    """ShaderLab string literals have no escapes: keep them on one line without double quotes."""
    return " ".join(str(value).replace('"', "'").split())


def _number(value: object) -> str:
    number = float(value) if isinstance(value, (int, float)) else 0.0
    return str(int(number)) if number.is_integer() else repr(number)


def _tag_pairs(tag_map: object) -> list[tuple[str, str]]:
    """SerializedTagMap.tags is a list of [key, value] pairs; older exports used {first, second}."""
    entries = tag_map.get("tags", []) if isinstance(tag_map, dict) else []
    pairs: list[tuple[str, str]] = []
    for entry in entries or []:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            pairs.append((str(entry[0]), str(entry[1])))
        elif isinstance(entry, dict) and "first" in entry:
            pairs.append((str(entry["first"]), str(entry.get("second", ""))))
    return pairs


def _render_tags(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    return "Tags { " + " ".join(f'"{_text(k)}"="{_text(v)}"' for k, v in pairs) + " }"


def _default(prop: dict, index: int) -> float:
    value = prop.get(f"m_DefValue[{index}]")
    if value is None:  # list form, as some exporters write it
        values = prop.get("m_DefValue")
        value = values[index] if isinstance(values, list) and len(values) > index else 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0


def _render_property(prop: dict) -> str:
    name = str(prop.get("m_Name", ""))
    desc = _text(prop.get("m_Description", name))
    flags = prop.get("m_Flags", 0) if isinstance(prop.get("m_Flags"), int) else 0
    attrs = [f"[{a}]" for a in prop.get("m_Attributes", []) or [] if isinstance(a, str) and a]
    attrs += [f"[{label}]" for bit, label in _PROP_FLAGS if flags & bit]
    prefix = " ".join(attrs) + (" " if attrs else "")
    kind = prop.get("m_Type")
    v = [_default(prop, i) for i in range(4)]
    if kind == _PROP_TEXTURE:
        texture = prop.get("m_DefTexture", {}) if isinstance(prop.get("m_DefTexture"), dict) else {}
        dim = _TEX_DIMS.get(texture.get("m_TexDim"), "2D")
        default = _text(texture.get("m_DefaultName") or "")
        return f'{prefix}{name} ("{desc}", {dim}) = "{default}" {{}}'
    if kind == _PROP_COLOR:
        return f'{prefix}{name} ("{desc}", Color) = ({", ".join(_number(x) for x in v)})'
    if kind == _PROP_VECTOR:
        return f'{prefix}{name} ("{desc}", Vector) = ({", ".join(_number(x) for x in v)})'
    if kind == _PROP_RANGE:
        return f'{prefix}{name} ("{desc}", Range({_number(v[1])}, {_number(v[2])})) = {_number(v[0])}'
    if kind == _PROP_INT:
        return f'{prefix}{name} ("{desc}", Integer) = {_number(v[0])}'
    if kind == _PROP_FLOAT:
        return f'{prefix}{name} ("{desc}", Float) = {_number(v[0])}'
    raise ValueError(f"property {name} has unknown serialized type {kind!r}")


def _render_properties(props: list[dict]) -> str:
    lines = ["Properties", "{"]
    lines += ["    " + _render_property(prop) for prop in props]
    lines.append("}")
    return "\n".join(lines)


def _value(state: dict, key: str) -> tuple[float, str | None]:
    """A SerializedShaderFloatValue: the baked value, or the property name it reads at runtime."""
    entry = state.get(key)
    if not isinstance(entry, dict):
        return 0.0, None
    name = entry.get("name")
    value = entry.get("val", 0.0)
    return (float(value) if isinstance(value, (int, float)) else 0.0,
            name if isinstance(name, str) and name not in _UNBOUND else None)


def _state_arg(state: dict, key: str, names: dict[int, str] | None = None) -> str:
    value, prop = _value(state, key)
    if prop is not None:
        return f"[{prop}]"
    return names.get(int(value), _number(value)) if names else _number(value)


def _bound(state: dict, *keys: str) -> bool:
    return any(_value(state, key)[1] is not None for key in keys)


def _color_mask(state: dict) -> str | None:
    value, prop = _value(state, "colMask")
    if prop is not None:
        return f"[{prop}]"
    mask = int(value)
    if mask == 15:
        return None
    return "".join(ch for bit, ch in ((8, "R"), (4, "G"), (2, "B"), (1, "A")) if mask & bit) or "0"


def _render_blend(state: dict, target: int, separate: bool) -> list[str]:
    blend = state.get(f"rtBlend{target}")
    if not isinstance(blend, dict):
        return []
    suffix = f" {target}" if separate else ""
    lines: list[str] = []
    keys = ("srcBlend", "destBlend", "srcBlendAlpha", "destBlendAlpha")
    src, dst, src_a, dst_a = (_value(blend, k) for k in keys)
    if _bound(blend, *keys) or (src[0], dst[0], src_a[0], dst_a[0]) != (1.0, 0.0, 1.0, 0.0):
        args = [_state_arg(blend, k, _BLEND) for k in keys]
        rgb, alpha = " ".join(args[:2]), " ".join(args[2:])
        lines.append(f"Blend{suffix} {rgb}" + ("" if rgb == alpha else f", {alpha}"))
    if _bound(blend, "blendOp", "blendOpAlpha") or _value(blend, "blendOp")[0] or _value(blend, "blendOpAlpha")[0]:
        rgb, alpha = (_state_arg(blend, k, _BLEND_OP) for k in ("blendOp", "blendOpAlpha"))
        lines.append(f"BlendOp{suffix} {rgb}" + ("" if rgb == alpha else f", {alpha}"))
    mask = _color_mask(blend)
    if mask is not None:
        lines.append(f"ColorMask {mask}{suffix}")
    return lines


def _render_stencil(state: dict) -> list[str]:
    ops = {"": state.get("stencilOp"), "Front": state.get("stencilOpFront"), "Back": state.get("stencilOpBack")}
    ops = {k: v for k, v in ops.items() if isinstance(v, dict)}
    general = ops.get("")
    ref, read, write = (_value(state, k) for k in ("stencilRef", "stencilReadMask", "stencilWriteMask"))
    def active(op: dict) -> bool:
        return (_bound(op, "comp", "pass", "fail", "zFail") or _value(op, "comp")[0] not in (0.0, 8.0)
                or any(_value(op, k)[0] for k in ("pass", "fail", "zFail")))
    used = any(active(op) for op in ops.values()) or _bound(state, "stencilRef", "stencilReadMask", "stencilWriteMask") \
        or ref[0] or read[0] not in (0.0, 255.0) or write[0] not in (0.0, 255.0)
    if not used:
        return []
    lines = ["Stencil", "{", f"    Ref {_state_arg(state, 'stencilRef')}"]
    for key, label in (("stencilReadMask", "ReadMask"), ("stencilWriteMask", "WriteMask")):
        value, prop = _value(state, key)
        if prop is not None or value not in (0.0, 255.0):
            lines.append(f"    {label} {_state_arg(state, key)}")
    for side, op in ops.items():
        if side and (op == general or not active(op)):
            continue
        lines.append(f"    Comp{side} {_state_arg(op, 'comp', _COMPARE)}")
        for key, label in (("pass", "Pass"), ("fail", "Fail"), ("zFail", "ZFail")):
            lines.append(f"    {label}{side} {_state_arg(op, key, _STENCIL_OP)}")
    lines.append("}")
    return lines


def _render_state(state: dict) -> list[str]:
    """Fixed-function render state; values bound to a material property render as [_Property]."""
    if not state:
        return []
    lines = [f"Cull {_state_arg(state, 'culling', _CULL)}",
             f"ZWrite {_state_arg(state, 'zWrite', {0: 'Off', 1: 'On'})}",
             f"ZTest {_state_arg(state, 'zTest', _COMPARE)}"]
    separate = bool(state.get("rtSeparateBlend"))
    for target in range(8 if separate else 1):
        lines.extend(_render_blend(state, target, separate))
    if _bound(state, "offsetFactor", "offsetUnits") or _value(state, "offsetFactor")[0] or _value(state, "offsetUnits")[0]:
        lines.append(f"Offset {_state_arg(state, 'offsetFactor')}, {_state_arg(state, 'offsetUnits')}")
    if _bound(state, "alphaToMask") or _value(state, "alphaToMask")[0]:
        lines.append(f"AlphaToMask {_state_arg(state, 'alphaToMask', {0: 'Off', 1: 'On'})}")
    if _bound(state, "conservative") or _value(state, "conservative")[0]:
        lines.append(f"Conservative {_state_arg(state, 'conservative', {0: 'False', 1: 'True'})}")
    lines.extend(_render_stencil(state))
    return lines


_STUB_PROGRAM = [
    "CGPROGRAM",
    "// Compiled sub-programs are not recoverable from the type tree; this is a stub, not the original.",
    "#pragma vertex vert",
    "#pragma fragment frag",
    '#include "UnityCG.cginc"',
    "",
    "struct appdata { float4 vertex : POSITION; float2 uv : TEXCOORD0; };",
    "struct v2f { float4 vertex : SV_POSITION; float2 uv : TEXCOORD0; };",
    "",
    "v2f vert(appdata v) { v2f o; o.vertex = UnityObjectToClipPos(v.vertex); o.uv = v.uv; return o; }",
    "fixed4 frag(v2f i) : SV_Target { return fixed4(1, 0, 1, 1); /* placeholder */ }",
    "ENDCG",
]


def _render_pass(p: dict, subshader_tags: list[tuple[str, str]]) -> str:
    kind = p.get("m_Type", _PASS_NORMAL)
    if kind == _PASS_USE:
        return f'UsePass "{_text(p.get("m_UseName", ""))}"'
    if kind == _PASS_GRAB:
        texture = _text(p.get("m_TextureName", ""))
        return "GrabPass { " + (f'"{texture}" ' if texture else "") + "}"
    state = p.get("m_State", {}) or {}
    lines: list[str] = []
    name = p.get("m_Name") or state.get("m_Name") or ""
    if name:
        lines.append(f'Name "{_text(name)}"')
    # Pass tags (e.g. LightMode) may be serialized on the state; tags inherited from the SubShader are not repeated.
    tags = _tag_pairs(p.get("m_Tags"))
    tags += [t for t in _tag_pairs(state.get("m_Tags")) if t not in tags and t not in subshader_tags]
    if tags:
        lines.append(_render_tags(tags))
    lines.extend(_render_state(state))
    lines.extend(_STUB_PROGRAM)
    return "\n".join(lines)


def _render_subshader(ss: dict) -> str:
    lines: list[str] = []
    tags = _tag_pairs(ss.get("m_Tags"))
    if tags:
        lines.append(_render_tags(tags))
    lod = ss.get("m_LOD", 0)
    if lod:
        lines.append(f"LOD {lod}")
    for p in ss.get("m_Passes", []) or []:
        body = _render_pass(p, tags)
        lines.append(body if p.get("m_Type") in (_PASS_USE, _PASS_GRAB) else f"Pass\n{{\n{_indent(body)}\n}}")
    return "\n".join(lines)


def _reconstruct_shaderlab(shader_data: dict) -> str:
    parsed = shader_data.get("m_ParsedForm", {}) or {}
    shader_name = parsed.get("m_Name", "Unknown/Recovered")
    lines: list[str] = [f'Shader "{_text(shader_name)}"', "{"]
    props = parsed.get("m_PropInfo", {}) or {}
    prop_list = props.get("m_Props", []) or []
    if prop_list:
        lines.append(_indent(_render_properties(prop_list)))
    subshaders = parsed.get("m_SubShaders", []) or []
    for ss in subshaders:
        body = _render_subshader(ss)
        lines.append(f"\n    SubShader\n    {{\n{_indent(body, '        ')}\n    }}")
    fallback = parsed.get("m_FallbackName", "")
    if fallback:
        lines.append(f'    Fallback "{_text(fallback)}"')
    custom_editor = parsed.get("m_CustomEditorName", "")
    if custom_editor:
        lines.append(f'    CustomEditor "{_text(custom_editor)}"')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _safe_stem(name: str, max_len: int = 80) -> str:
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    return safe.strip("_. ")[:max_len] or "shader"


def recover_shaders(source_dir: Path, out_dir: Path) -> ShaderResult:
    """Scan for shader JSON files and recover ShaderLab source."""
    if not source_dir.is_dir():
        raise UareError(f"{source_dir} is not a directory")
    # The first folder that exists is the only search root (never a union with the whole package tree).
    search_root = next(d for d in (source_dir / "shared" / "shaders", source_dir / "shaders", source_dir)
                       if d.is_dir())
    shader_jsons = sorted(search_root.rglob("*.json"))

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
