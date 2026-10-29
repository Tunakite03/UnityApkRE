"""Run a caller-supplied external transform over a file (used for opt-in decryption/unpacking).

The command is split with shlex and never goes through a shell. Tokens `{input}` and `{output}`
are replaced with a scratch input copy and a scratch output path. If the command contains no
`{output}` token, its stdout is taken as the result. A non-zero exit or exceeding the timeout is
an error; on timeout the command is killed.
"""

import logging
import shlex
import subprocess
import tempfile
from pathlib import Path

from uare.errors import UareError
from uare.logs import event

LOG = logging.getLogger("uare.external")

_MAX_STDERR = 400
DEFAULT_TIMEOUT_S = 1800.0


def run_command(command: str, data: bytes, timeout_s: float) -> bytes:
    """Feed `data` to `command` and return its transformed output within `timeout_s` seconds."""
    argv = shlex.split(command)
    if not argv:
        raise UareError("empty external command")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input"
        dst = Path(tmp) / "output"
        src.write_bytes(data)
        wants_output = any("{output}" in arg for arg in argv)
        argv = [arg.replace("{input}", str(src)).replace("{output}", str(dst)) for arg in argv]
        event(LOG, logging.INFO, "external_command", argv0=argv[0], bytes=len(data))
        try:
            proc = subprocess.run(argv, capture_output=True, check=False, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise UareError(f"external command {argv[0]} exceeded {timeout_s:g}s and was killed; "
                            "raise --cmd-timeout if it is still making progress") from exc
        except OSError as exc:
            raise UareError(f"external command could not run: {exc}") from exc
        if proc.returncode != 0:
            raise UareError(f"external command exited {proc.returncode}: {proc.stderr[:_MAX_STDERR]!r}")
        if wants_output:
            if not dst.is_file():
                raise UareError("external command did not write its {output}")
            return dst.read_bytes()
        return proc.stdout
