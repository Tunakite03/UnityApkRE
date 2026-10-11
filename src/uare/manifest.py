"""Run manifest: `<workdir>/manifest.json`, written at the end of every `uare run`.

The manifest says what the run did and what it did not do, using only facts read from the files
the run produced. Every path is relative to the work directory. Sections:

    status      complete | partial | failed, with the reason in status_reason
    run         start and finish times (UTC) and the duration of each stage
    stages      one entry per pipeline stage: ok, failed or not_run, with the error text if any
    package     id, version and device serial (from pull/manifest.json)
    inputs      each APK split with its sha256 and size
    outputs     each artifact the run wrote: exists, bytes, sha256
    unity       Unity versions, scripting backend, IL2CPP metadata version and counts
    assets      object counts from extracted-assets/report.json
    coverage    what was decoded, what was not, and why (hot-update assemblies, FMOD banks, skipped objects)
    scope       what this tool never does here: decryption and decompilation (always empty lists)
    limitations plain-language statements derived from coverage

A stage that fails is recorded with its error, and the manifest is still written. Stages that never
started are listed as not_run, so a reader can see where the run stopped.
"""

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from uare import __version__

SCHEMA = "uare.run-manifest"
SCHEMA_VERSION = 1
STAGE_NAMES = ("pull", "extract", "analyze", "package", "blueprint")
MANIFEST_NAME = "manifest.json"

T = TypeVar("T")
_DIGITS = re.compile(r"\d+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StageRecord:
    name: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        start = datetime.fromisoformat(self.started_at)
        finish = datetime.fromisoformat(self.finished_at)
        return round((finish - start).total_seconds(), 1)


class RunLog:
    """Records each stage of one run. Stages not reached stay `not_run`."""

    def __init__(self) -> None:
        self.started_at = _now()
        self._records: dict[str, StageRecord] = {}

    def stage(self, name: str, action: Callable[[], T]) -> T:
        record = StageRecord(name=name, status="running", started_at=_now())
        self._records[name] = record
        try:
            result = action()
        except Exception as exc:  # recorded in the manifest, then re-raised so the command still fails
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            record.finished_at = _now()
            raise
        record.status = "ok"
        record.finished_at = _now()
        return result

    def stages(self) -> list[StageRecord]:
        return [self._records.get(name, StageRecord(name=name, status="not_run")) for name in STAGE_NAMES]


def _read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact(workdir: Path, role: str, rel: str) -> dict[str, Any]:
    path = workdir / rel
    if not path.is_file():
        return {"role": role, "path": rel, "exists": False, "bytes": None, "sha256": None}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return {"role": role, "path": rel, "exists": True, "bytes": path.stat().st_size,
            "sha256": digest.hexdigest()}


def _hot_update_assemblies(catalog: list[dict[str, Any]], workdir: Path) -> list[dict[str, Any]]:
    entries = []
    for item in catalog:
        if item.get("category") != "scripts" or not item.get("output"):
            continue
        path = workdir / "extracted-assets" / item["output"]
        head = path.read_bytes()[:2] if path.is_file() else b""
        entries.append({"path": f"extracted-assets/{item['output']}", "bytes": path.stat().st_size
                        if path.is_file() else None, "format": "pe" if head == b"MZ" else "not-pe"})
    return entries


def _fmod_banks(catalog: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in catalog:
        if item.get("class_name") != "TextAsset" or item.get("category") != "audio":
            continue
        note = item.get("note") or ""
        if "subsounds decoded" in note:
            counts["with_samples"] += 1
        elif "event metadata only" in note:
            counts["metadata_only"] += 1
        else:
            counts["other"] += 1
    counts["total"] = sum(counts.values())
    return dict(counts)


def _skipped_by_reason(catalog: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in catalog:
        if item.get("output") is None:
            counts[_DIGITS.sub("N", item.get("note") or "no reason recorded")[:120]] += 1
    return dict(counts.most_common())


def _status(stages: list[StageRecord], assets: dict[str, Any] | None) -> tuple[str, str]:
    failed = [s.name for s in stages if s.status == "failed"]
    not_run = [s.name for s in stages if s.status == "not_run"]
    if failed or not_run:
        return "failed", f"stage(s) failed: {failed or '-'}; not run: {not_run or '-'}"
    if assets is None:
        return "failed", "the package stage left no report"
    if assets.get("errors"):
        return "partial", f"{assets['errors']} file(s) could not be opened"
    if assets.get("skipped"):
        return "partial", f"{assets['skipped']} object(s) produced no output; see the catalog notes"
    return "complete", "analysis pipeline completed; this does not establish reconstruction or gameplay completeness"


def _limitations(coverage: dict[str, Any], assets: dict[str, Any] | None) -> list[str]:
    notes: list[str] = []
    opaque = [a for a in coverage["hot_update_assemblies"] if a["format"] != "pe"]
    if opaque:
        notes.append(f"{len(opaque)} hot-update assembly file(s) are not plain .NET assemblies; "
                     "they are stored as found and not decrypted")
    banks = coverage["fmod_banks"]
    if banks.get("metadata_only"):
        notes.append(f"{banks['metadata_only']} FMOD bank(s) hold event metadata only; no sample data to decode")
    if coverage["unexported_objects"]["total"]:
        notes.append(f"{coverage['unexported_objects']['total']} object(s) have no output file; "
                     "their reasons are grouped in coverage.unexported_objects.by_reason")
    if assets is not None and assets.get("in_hierarchy"):
        notes.append(f"{assets['in_hierarchy']} component(s) are stored inside prefab or scene JSON, not as files")
    return notes


def build_manifest(workdir: Path, run: RunLog) -> dict[str, Any]:
    stages = run.stages()
    pull = _read_json(workdir / "pull" / "manifest.json")
    analysis = _read_json(workdir / "analysis" / "report.json")
    assets = _read_json(workdir / "extracted-assets" / "report.json")
    catalog = _read_json(workdir / "extracted-assets" / "catalog.json") or []

    status, status_reason = _status(stages, assets)
    finished_at = _now()
    package = None
    inputs: list[dict[str, Any]] = []
    if pull is not None:
        package = {"id": pull.get("package"), "version_name": pull.get("version", {}).get("version_name"),
                   "version_code": pull.get("version", {}).get("version_code"),
                   "device_serial": pull.get("device_serial")}
        inputs = [{"kind": f["kind"], "path": f"pull/{f['local_path']}", "bytes": f["size"], "sha256": f["sha256"]}
                  for f in pull.get("files", [])]

    unity = None
    if analysis is not None:
        inventory = analysis.get("inventory", {})
        il2cpp = analysis.get("il2cpp", {})
        unity = {
            "unity_versions": inventory.get("unity_versions"),
            "target_platforms": inventory.get("target_platforms"),
            "scripting_backend": analysis.get("scripting_backend"),
            "il2cpp": {"status": il2cpp.get("status"), "metadata_version": il2cpp.get("version"),
                       "identifiers": il2cpp.get("identifiers"), "string_literals": il2cpp.get("string_literals")},
            "scene_files": inventory.get("scene_files"),
            "native_libraries": [{"abi": lib.get("abi"), "name": lib.get("name")}
                                 for lib in analysis.get("native_libraries", [])],
        }

    coverage = {
        "hot_update_assemblies": _hot_update_assemblies(catalog, workdir),
        "fmod_banks": _fmod_banks(catalog),
        "unexported_objects": {"total": sum(1 for item in catalog if item.get("output") is None),
                               "by_reason": _skipped_by_reason(catalog)},
    }

    artifacts = [
        _artifact(workdir, "game_model", "extracted-assets/game_model.json"),
        _artifact(workdir, "blueprint_report", "blueprint/report.json"),
        _artifact(workdir, "blueprint_browser", "blueprint/index.html"),
        _artifact(workdir, "pull_manifest", "pull/manifest.json"),
        _artifact(workdir, "extraction", "extracted/extraction.json"),
        _artifact(workdir, "analysis_report", "analysis/report.json"),
        _artifact(workdir, "asset_report", "extracted-assets/report.json"),
        _artifact(workdir, "asset_catalog", "extracted-assets/catalog.json"),
        _artifact(workdir, "asset_index", "extracted-assets/index.html"),
    ]

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "status_reason": status_reason,
        "tool": {"name": "uare", "version": __version__},
        "run": {"started_at": run.started_at, "finished_at": finished_at,
                "workdir": "."},
        "stages": [{"name": s.name, "status": s.status, "started_at": s.started_at,
                    "finished_at": s.finished_at, "duration_s": s.duration_s, "error": s.error}
                   for s in stages],
        "package": package,
        "inputs": inputs,
        "outputs": artifacts,
        "unity": unity,
        "assets": None if assets is None else {k: assets.get(k) for k in (
            "files", "objects", "exported", "in_hierarchy", "skipped", "errors", "categories", "scenes")},
        "coverage": coverage,
        "scope": {"decrypted": [], "decompiled": []},
        "limitations": _limitations(coverage, assets),
    }


def write_manifest(workdir: Path, run: RunLog) -> dict[str, Any]:
    manifest = build_manifest(workdir, run)
    (workdir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def summarize(manifest: dict[str, Any]) -> list[str]:
    lines = [f"manifest          : {manifest['status']} - {manifest['status_reason']}"]
    for stage in manifest["stages"]:
        detail = f" ({stage['error']})" if stage["error"] else ""
        lines.append(f"  {stage['name']:<10}: {stage['status']}{detail}")
    for note in manifest["limitations"]:
        lines.append(f"limitation        : {note}")
    return lines
