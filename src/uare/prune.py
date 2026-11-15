"""Remove superseded intermediate outputs from a game workspace.

Every producing command writes into a new, empty directory, so a workspace grows with each re-run.
Once a newer output has been verified, the older copies of the same stage are dead weight. `uare prune`
lists them (dry run by default) and, with `--yes`, removes them.

Removal is scoped to the families of the kept directories: `--keep extracted-assets-game` replaces the
other `extracted-assets*` outputs and leaves every other family alone, so pruning right after one stage
never deletes the current output of another. Raw captures, the decrypted/decompiled evidence, the
Ghidra output and anything outside the workspace are never touched. Failed staging folders of a kept
family (`.<member>.*.partial`, left by a crash) are candidates too.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError
from uare.logs import event

LOG = logging.getLogger("uare.prune")

# Intermediate output families, one per stage. A directory is a member when its name equals one of the
# family's prefixes or extends it with "-" or a digit (so "extracted-assets-game" and "ar-try1" match,
# "extracted" does not). AssetRipper attempts share one family whatever prefix they were given.
PRUNABLE_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("extracted-assets",),
    ("assetripper", "ar-try", "ar-diag"),
    ("export-assets",),
    ("il2cpp-annotated",),
    ("il2cpp-tables",),
    ("il2cpp-enriched",),
    ("sprite-sheets",),
)
_STAGING_SUFFIX = ".partial"


class PruneError(UareError):
    """The prune request names a directory that is not in the workspace or would remove the kept output."""


@dataclass(frozen=True)
class PrunePlan:
    workdir: str
    keep: tuple[str, ...]
    remove: tuple[str, ...]
    kept: tuple[str, ...]


def _family_of(name: str) -> tuple[str, ...] | None:
    for family in PRUNABLE_FAMILIES:
        for prefix in family:
            if name == prefix or (name.startswith(prefix) and name[len(prefix)] in "-0123456789"):
                return family
    return None


def _staging_member(name: str) -> str | None:
    """The output name a crashed `.<member>.<suffix>.partial` staging folder belonged to."""
    if not (name.startswith(".") and name.endswith(_STAGING_SUFFIX)):
        return None
    return name[1:].split(".", 1)[0]


def plan_prune(workdir: Path, keep: tuple[str, ...]) -> PrunePlan:
    """Decide what to remove. Every name in `keep` must be a directory in `workdir` of a prunable family."""
    root = workdir.resolve()
    if not root.is_dir():
        raise PruneError(f"workspace {workdir} is not a directory")
    families: set[tuple[str, ...]] = set()
    for name in keep:
        if "/" in name or name in (".", ".."):
            raise PruneError(f"--keep takes a directory name inside the workspace, not {name!r}")
        if not (root / name).is_dir() or (root / name).is_symlink():
            raise PruneError(f"--keep {name!r} is not a directory in {root}; refusing to prune without it")
        family = _family_of(name)
        if family is None:
            raise PruneError(f"--keep {name!r} is not an intermediate output family; nothing it could replace")
        families.add(family)
    remove: list[str] = []
    kept: list[str] = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        if entry.name in keep:
            kept.append(entry.name)
            continue
        staged = _staging_member(entry.name)
        if _family_of(staged if staged is not None else entry.name) in families:
            remove.append(entry.name)
    return PrunePlan(str(root), tuple(keep), tuple(remove), tuple(kept))


def apply_prune(plan: PrunePlan) -> list[str]:
    """Delete the planned directories. Symlinks inside them are removed, never followed."""
    root = Path(plan.workdir)
    removed: list[str] = []
    for name in plan.remove:
        target = root / name
        if target.is_symlink() or not target.is_dir():
            continue
        shutil.rmtree(target)
        removed.append(name)
        event(LOG, logging.INFO, "pruned_output", directory=name)
    return removed


def summarize(plan: PrunePlan, removed: list[str] | None) -> list[str]:
    lines = [
        f"workspace         : {plan.workdir}",
        f"keep              : {', '.join(plan.keep) or '(none)'}",
        f"candidates        : {len(plan.remove)} -> {', '.join(plan.remove) or '(none)'}",
    ]
    if removed is None:
        lines.append("dry run           : nothing deleted; rerun with --yes to remove the candidates")
    else:
        lines.append(f"removed           : {len(removed)}")
    return lines
