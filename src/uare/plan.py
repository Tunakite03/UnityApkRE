"""Dry-run plan for a pipeline: the ordered steps, the exact commands, what each step writes, and the
local prerequisites that are missing. Building a plan never touches a device and never writes files,
so an agent can read it and decide what to run without operating anything.
"""

from dataclasses import dataclass
from pathlib import Path

_ASSET_MODULES = ("UnityPy", "PIL", "fmod_toolkit")


@dataclass(frozen=True)
class Prerequisites:
    """Local facts gathered by the caller. `adb_error` is set when no adb could be resolved."""

    adb: str | None
    adb_source: str | None
    adb_error: str | None
    missing_modules: tuple[str, ...]
    workdir_state: str  # "absent" | "empty" | "populated"


@dataclass(frozen=True)
class Step:
    name: str
    command: str
    writes: str
    needs_device: bool
    needs_unitypy: bool


@dataclass(frozen=True)
class Plan:
    package: str
    workdir: str
    ready: bool
    blockers: tuple[str, ...]
    steps: tuple[Step, ...]
    checks: tuple[str, ...]


def asset_modules() -> tuple[str, ...]:
    return _ASSET_MODULES


def workdir_state(path: Path) -> str:
    if not path.exists():
        return "absent"
    if path.is_dir() and not any(path.iterdir()):
        return "empty"
    return "populated"


def build_plan(package: str, workdir: Path, prerequisites: Prerequisites) -> Plan:
    wd = str(workdir)
    steps = (
        Step("pull", f"scripts/uare pull {package} --out {wd}/pull",
             f"{wd}/pull/ (APK splits, OBBs, manifest.json)", needs_device=True, needs_unitypy=False),
        Step("extract", f"scripts/uare extract {wd}/pull --out {wd}/extracted",
             f"{wd}/extracted/ (merged tree: Data/, lib/, assets/, extraction.json)",
             needs_device=False, needs_unitypy=False),
        Step("analyze", f"scripts/uare analyze {wd}/extracted --out {wd}/analysis",
             f"{wd}/analysis/ (report.json, il2cpp/, files.jsonl)", needs_device=False, needs_unitypy=False),
        Step("package", f"scripts/uare package {wd}/extracted --out {wd}/extracted-assets",
             f"{wd}/extracted-assets/ (assets by category and bundle, prefabs, scenes, catalog.json)",
             needs_device=False, needs_unitypy=True),
    )
    blockers: list[str] = []
    if prerequisites.adb_error is not None:
        blockers.append(f"adb: {prerequisites.adb_error}")
    if prerequisites.missing_modules:
        blockers.append("UnityPy extra missing (needed by the package step): "
                        + ", ".join(prerequisites.missing_modules)
                        + "; run `pip install 'uare[power]'` or set UARE_PYTHON to an interpreter that has it")
    if prerequisites.workdir_state == "populated":
        blockers.append(f"workdir {wd} is not empty; use a new directory, outputs are never overwritten")
    checks = (
        "scripts/run-tests  (unit tests; no device, no real data)",
        "UARE_REAL_DATA_DIR=<workdir>/extracted/Data scripts/run-tests  (adds the real-data smoke test after an analyze)",
        "catalog.json: every entry has an output or a note; report.json open errors is 0",
        "scripts/uare summary <workdir>/analysis  (sanity check of the analysis)",
    )
    return Plan(package=package, workdir=wd, ready=not blockers, blockers=tuple(blockers),
                steps=steps, checks=checks)


def summarize(plan: Plan) -> list[str]:
    lines = [
        f"package           : {plan.package}",
        f"workdir           : {plan.workdir}",
        f"ready             : {'yes' if plan.ready else 'no'}",
    ]
    lines += [f"blocker           : {item}" for item in plan.blockers]
    lines.append("steps:")
    for index, step in enumerate(plan.steps, 1):
        flags = []
        if step.needs_device:
            flags.append("needs device")
        if step.needs_unitypy:
            flags.append("needs UnityPy")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {index}. {step.name}{suffix}")
        lines.append(f"     command: {step.command}")
        lines.append(f"     writes : {step.writes}")
    lines.append("checks after the run:")
    lines += [f"  - {check}" for check in plan.checks]
    return lines
