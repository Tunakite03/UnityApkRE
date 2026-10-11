"""Versioned, loss-aware object identities and serialized reference resolution.

The model describes the supplied build, not the original Editor project. Object
IDs include the container and serialized file; a path ID alone is never global.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, TypeAlias

from uare.errors import UareError

Json: TypeAlias = "None | bool | int | float | str | list[Json] | dict[str, Json]"
SCHEMA_VERSION = 1


class ModelError(UareError):
    """The reconstruction model is malformed or internally inconsistent."""


def json_value(value: object, context: str) -> Json:
    """Validate a library/JSON boundary without coercing unsupported objects."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [json_value(item, context) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: json_value(item, context) for key, item in value.items()}
    raise ModelError(f"Cannot preserve serialized data: unsupported {type(value).__name__}; source={context}")


def record(value: object, context: str) -> dict[str, Json]:
    validated = json_value(value, context)
    if not isinstance(validated, dict):
        raise ModelError(f"Expected a JSON object; source={context}")
    return validated


def array(value: Json, context: str) -> list[Json]:
    if not isinstance(value, list):
        raise ModelError(f"Expected a JSON array; source={context}")
    return value


def string(value: Json, context: str) -> str:
    if not isinstance(value, str):
        raise ModelError(f"Expected a string; source={context}")
    return value


def integer(value: Json, context: str) -> int:
    if type(value) is not int:
        raise ModelError(f"Expected an integer; source={context}")
    return value


def read_json(path: Path) -> dict[str, Json]:
    try:
        return record(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ModelError(f"Cannot read reconstruction data: {type(exc).__name__}; source={path}") from exc


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # UnityPy can represent binary TextAsset bytes with surrogate escapes.
    # JSON escapes preserve those code points without invalid UTF-8 output.
    path.write_text(json.dumps(json_value(value, str(path)), ensure_ascii=True,
                               indent=2, allow_nan=False) + "\n", encoding="utf-8")


def identity(source: str, serialized_file: str, path_id: int) -> str:
    payload = json.dumps([source, serialized_file, path_id], ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ObjectRecord:
    id: str
    source: str
    serialized_file: str
    path_id: int
    class_name: str
    name: str
    data: dict[str, Json]
    data_status: str
    issues: tuple[str, ...]
    output: str | None


@dataclass(frozen=True)
class FileRecord:
    source: str
    serialized_file: str
    externals: tuple[str, ...]


@dataclass(frozen=True)
class Reference:
    source_id: str
    field: str
    file_id: int
    path_id: int
    target_id: str | None
    status: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class GameModel:
    files: tuple[FileRecord, ...]
    objects: tuple[ObjectRecord, ...]
    references: tuple[Reference, ...]
    issues: tuple[str, ...]


def pointers(value: Json, field: str) -> Iterator[tuple[str, int, int]]:
    if isinstance(value, dict):
        if "m_FileID" in value and "m_PathID" in value:
            yield field, integer(value["m_FileID"], field), integer(value["m_PathID"], field)
        else:
            for key, item in value.items():
                yield from pointers(item, f"{field}.{key}" if field else key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from pointers(item, f"{field}[{index}]")


def resolve(files: tuple[FileRecord, ...], objects: tuple[ObjectRecord, ...],
            issues: tuple[str, ...]) -> GameModel:
    file_map = {(f.source, f.serialized_file): f for f in files}
    by_key = {(o.source, o.serialized_file, o.path_id): o for o in objects}
    if len(file_map) != len(files) or len(by_key) != len(objects):
        raise ModelError("Cannot resolve model: duplicate file or object identity")
    by_name: dict[str, list[FileRecord]] = defaultdict(list)
    for file in files:
        by_name[posixpath.basename(file.serialized_file.replace("\\", "/")).casefold()].append(file)
    refs: list[Reference] = []
    for obj in objects:
        owner = file_map.get((obj.source, obj.serialized_file))
        if owner is None:
            raise ModelError(f"Cannot resolve model: object has no owning file; object={obj.id}")
        for field, fid, pid in pointers(obj.data, ""):
            targets: list[FileRecord] = []
            status = "null" if pid == 0 else "missing_file"
            if pid != 0 and fid == 0:
                targets = [owner]
            elif pid != 0 and 0 < fid <= len(owner.externals):
                external = owner.externals[fid - 1].replace("\\", "/")
                matches = by_name.get(posixpath.basename(external).casefold(), [])
                exact = [f for f in matches if f.source == external or
                         posixpath.normpath(posixpath.join(posixpath.dirname(owner.source), external)) == f.source]
                same_container = [f for f in matches if f.source == owner.source]
                targets = exact or same_container or matches
            elif pid != 0:
                status = "invalid_external_index"
            candidates = tuple(sorted(by_key[(f.source, f.serialized_file, pid)].id for f in targets
                                      if (f.source, f.serialized_file, pid) in by_key))
            if pid != 0 and targets:
                status = "ambiguous" if len(targets) > 1 else ("resolved" if candidates else "missing_object")
            target = candidates[0] if status == "resolved" else None
            refs.append(Reference(obj.id, field, fid, pid, target, status, candidates))
    return GameModel(tuple(sorted(files, key=lambda f: (f.source, f.serialized_file))),
                     tuple(sorted(objects, key=lambda o: o.id)),
                     tuple(sorted(refs, key=lambda r: (r.source_id, r.field))), issues)


def save_model(path: Path, model: GameModel) -> None:
    write_json(path, {"schema": "uare.game-model", "schema_version": SCHEMA_VERSION, **asdict(model)})


def load_model(path: Path) -> GameModel:
    raw = read_json(path)
    if raw.get("schema") != "uare.game-model" or type(raw.get("schema_version")) is not int or raw.get("schema_version") != SCHEMA_VERSION:
        raise ModelError(f"Unsupported game model schema; source={path}")
    files: list[FileRecord] = []
    objects: list[ObjectRecord] = []
    for value in array(raw.get("files"), "files"):
        row = record(value, "file")
        files.append(FileRecord(string(row.get("source"), "source"),
                                string(row.get("serialized_file"), "serialized_file"),
                                tuple(string(x, "external") for x in array(row.get("externals"), "externals"))))
    for value in array(raw.get("objects"), "objects"):
        row = record(value, "object")
        source = string(row.get("source"), "source")
        serialized = string(row.get("serialized_file"), "serialized_file")
        pid = integer(row.get("path_id"), "path_id")
        oid = string(row.get("id"), "id")
        if oid != identity(source, serialized, pid):
            raise ModelError(f"Object identity does not match its source; object={oid}")
        status = string(row.get("data_status"), "data_status")
        if status not in {"complete", "partial", "unreadable"}:
            raise ModelError(f"Invalid serialized data status; object={oid}")
        output = row.get("output")
        if output is not None:
            output = string(output, "output")
            if Path(output).is_absolute() or ".." in Path(output).parts or "\\" in output:
                raise ModelError(f"Unsafe exported asset path; object={oid}")
        objects.append(ObjectRecord(oid, source, serialized, pid,
                                    string(row.get("class_name"), "class_name"),
                                    string(row.get("name"), "name"), record(row.get("data"), "data"),
                                    status, tuple(string(x, "issue") for x in array(row.get("issues"), "issues")), output))
    # Edges are always recomputed from validated source fields, never trusted from a cached report.
    return resolve(tuple(files), tuple(objects), tuple(string(x, "issue") for x in array(raw.get("issues"), "issues")))


def outgoing(model: GameModel) -> dict[str, tuple[Reference, ...]]:
    result: dict[str, list[Reference]] = defaultdict(list)
    for ref in model.references:
        result[ref.source_id].append(ref)
    return {key: tuple(value) for key, value in result.items()}


def closure(seeds: set[str], edges: dict[str, tuple[Reference, ...]]) -> set[str]:
    visited = set(seeds)
    pending = list(seeds)
    while pending:
        for edge in edges.get(pending.pop(), ()):
            if edge.target_id is not None and edge.target_id not in visited:
                visited.add(edge.target_id)
                pending.append(edge.target_id)
    return visited
