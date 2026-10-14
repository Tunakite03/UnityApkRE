"""UnityPy boundary adapter; preserve fields before category exporters discard context."""

from __future__ import annotations

import base64
import math
import struct
from collections import defaultdict
from pathlib import Path
from typing import Protocol

from uare.game_model import (FileRecord, Json, ModelError, ObjectRecord, identity, json_value, non_finite_tag,
                             record)

READ_ERRORS = (EOFError, ValueError, OSError, KeyError, TypeError, AttributeError,
               NotImplementedError, RuntimeError, IndexError)


class UnityObject(Protocol):
    """The UnityPy object surface used by the capture/export boundary."""

    path_id: int

    def peek_name(self) -> str | None: ...

    def read(self) -> object: ...

    def read_typetree(self) -> object: ...


def serialized_name(obj: object, source: str) -> str:
    owner = getattr(obj, "assets_file", None)
    return str(getattr(owner, "name", "") or Path(source).name)


def group_objects(objects: list[UnityObject], source: str) -> dict[str, list[UnityObject]]:
    groups: dict[str, list[UnityObject]] = defaultdict(list)
    for obj in objects:
        groups[serialized_name(obj, source)].append(obj)
    return dict(groups)


def _value(value: object) -> Json:
    if isinstance(value, (bytes, bytearray)):
        return {"$binary_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        # Genuine serialized values: stepped-curve tangents, unbreakable joints, unused matrix slots.
        return non_finite_tag(value)
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ModelError("Serialized type tree contains non-string dictionary keys")
        return {key: _value(item) for key, item in value.items()}
    return json_value(value, "UnityPy type tree")


def _pointer(value: object) -> dict[str, Json]:
    return {"m_FileID": int(getattr(value, "m_FileID", getattr(value, "file_id", 0))),
            "m_PathID": int(getattr(value, "m_PathID", getattr(value, "path_id", 0)))}


def _monobehaviour_header(obj: object) -> dict[str, Json]:
    """Read the fixed MonoBehaviour prefix when no type tree describes the script's own fields.

    Layout for serialized format >= 14: PPtr m_GameObject (int32 file, int64 path), UInt8 m_Enabled
    aligned to 4, PPtr m_Script, then the m_Name string (int32 length + UTF-8 bytes).
    """
    version = int(getattr(getattr(getattr(obj, "assets_file"), "header"), "version"))
    if version < 14:
        raise NotImplementedError(f"MonoBehaviour header layout for serialized format {version}")
    raw = bytes(getattr(obj, "get_raw_data")())
    if len(raw) < 32:
        raise ValueError(f"MonoBehaviour data shorter than its header; size={len(raw)}")
    if raw[12] > 1 or raw[13:16] != b"\x00\x00\x00":
        # Editor-layout objects start with extra fields; refuse rather than mis-read a binding.
        raise ValueError("MonoBehaviour data does not match the release header layout")
    endian = "<" if getattr(getattr(obj, "reader"), "endian") == "<" else ">"
    go_file, go_path = struct.unpack_from(endian + "iq", raw, 0)
    script_file, script_path = struct.unpack_from(endian + "iq", raw, 16)
    (name_length,) = struct.unpack_from(endian + "i", raw, 28)
    if not 0 <= name_length <= len(raw) - 32:
        raise ValueError(f"MonoBehaviour name length exceeds object data; length={name_length}")
    return {"m_GameObject": {"m_FileID": go_file, "m_PathID": go_path}, "m_Enabled": raw[12],
            "m_Script": {"m_FileID": script_file, "m_PathID": script_path},
            "m_Name": raw[32:32 + name_length].decode("utf-8")}


def _fallback(obj: object, cls: str) -> dict[str, Json]:
    if cls == "MonoBehaviour":
        # UnityPy's typed read needs the script's type tree too; the header alone keeps the binding.
        return _monobehaviour_header(obj)
    data = getattr(obj, "read")()
    if cls == "MonoScript":
        return {key: str(getattr(data, key)) for key in ("m_ClassName", "m_Namespace", "m_AssemblyName")}
    if cls == "GameObject":
        result: dict[str, Json] = {"m_Name": str(getattr(data, "m_Name")),
                                   "m_Component": [{"component": _pointer(getattr(c, "component", c))}
                                                   for c in getattr(data, "m_Component")]}
        for key in ("m_Layer", "m_IsActive", "m_Tag", "m_TagString"):
            if hasattr(data, key):
                result[key] = _value(getattr(data, key))
        return result
    if cls in {"Transform", "RectTransform"}:
        result = {key: _pointer(getattr(data, key)) for key in ("m_GameObject", "m_Father")}
        if hasattr(data, "m_Children"):
            result["m_Children"] = [_pointer(c) for c in getattr(data, "m_Children")]
        for key in ("m_LocalPosition", "m_LocalRotation", "m_LocalScale", "m_AnchorMin", "m_AnchorMax",
                    "m_AnchoredPosition", "m_SizeDelta", "m_Pivot"):
            if hasattr(data, key):
                vector = getattr(data, key)
                result[key] = {axis: _value(float(getattr(vector, axis))) for axis in "xyzw" if hasattr(vector, axis)}
        return result
    return {}


def capture(source: str, serialized: str, objects: list[UnityObject],
            exports: dict[int, str]) -> tuple[FileRecord, tuple[ObjectRecord, ...]]:
    owner = getattr(objects[0], "assets_file", None) if objects else None
    externals = tuple(str(getattr(item, "path")) for item in getattr(owner, "externals", ()))
    records: list[ObjectRecord] = []
    for obj in objects:
        pid = int(getattr(obj, "path_id"))
        cls = str(getattr(getattr(obj, "type"), "name"))
        try:
            name = str(getattr(obj, "peek_name")() or "")
        except READ_ERRORS:
            name = ""
        issues: list[str] = []
        status = "complete"
        try:
            data = record(_value(getattr(obj, "read_typetree")()), identity(source, serialized, pid))
        except (*READ_ERRORS, ModelError) as exc:
            # Keep the failure type and location, never a decoder's dump of field values.
            issues.append(f"Type tree unavailable: {type(exc).__name__}")
            try:
                data = _fallback(obj, cls)
            except READ_ERRORS as fallback_error:
                data = {}
                issues.append(f"Typed read unavailable: {type(fallback_error).__name__}")
            status = "partial" if data else "unreadable"
        records.append(ObjectRecord(identity(source, serialized, pid), source, serialized, pid,
                                    cls, name, data, status, tuple(issues), exports.get(pid)))
    return FileRecord(source, serialized, externals), tuple(records)
