"""All-or-nothing output directories: work happens in a hidden sibling that is renamed on success."""

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from uare.errors import OutputDirectoryError


@contextmanager
def staged_output(out_dir: Path) -> Iterator[Path]:
    """Yield an empty staging directory; on success it replaces `out_dir`, on failure it is deleted.

    `out_dir` must be absent or an empty directory, so prior results are never overwritten.
    """
    if out_dir.exists():
        if not out_dir.is_dir():
            raise OutputDirectoryError(f"output path {out_dir} exists and is not a directory")
        if any(out_dir.iterdir()):
            raise OutputDirectoryError(f"output directory {out_dir} is not empty")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.", suffix=".partial", dir=out_dir.parent))
    try:
        yield staging
    except BaseException:
        shutil.rmtree(staging)
        raise
    if out_dir.exists():
        out_dir.rmdir()
    staging.rename(out_dir)
