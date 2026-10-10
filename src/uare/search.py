"""Regex search over the IL2CPP tables written by `uare analyze` (il2cpp/identifiers.tsv, string_literals.jsonl).

The metadata string heap stores names roughly in definition order, so the lines around a
class name usually hold its namespace, fields and methods; `context` exposes them.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from uare.errors import UareError

MAX_DISPLAY = 300


@dataclass(frozen=True)
class Hit:
    line: int  # 0-based row in the table
    key: str  # heap offset for identifiers, literal index for literals
    value: str
    is_match: bool


def _lines(path: Path) -> list[str]:
    """Rows split on '\\n' only; str.splitlines would also split on U+0085/U+2028 inside game text."""
    text = path.read_text(encoding="utf-8")
    return text[:-1].split("\n") if text.endswith("\n") else (text.split("\n") if text else [])


def _identifier_rows(analysis_dir: Path) -> list[tuple[str, str]]:
    path = analysis_dir / "il2cpp" / "identifiers.tsv"
    if not path.is_file():
        raise UareError(f"{path} not found; run `uare analyze` on an IL2CPP build first")
    rows: list[tuple[str, str]] = []
    for number, line in enumerate(_lines(path)):
        offset, sep, text = line.partition("\t")
        if not sep:
            raise UareError(f"{path}:{number + 1} is not offset<TAB>name")
        rows.append((offset, text))
    return rows


def _literal_rows(analysis_dir: Path) -> list[tuple[str, str]]:
    path = analysis_dir / "il2cpp" / "string_literals.jsonl"
    if not path.is_file():
        raise UareError(f"{path} not found; run `uare analyze` on an IL2CPP build first")
    rows: list[tuple[str, str]] = []
    for number, line in enumerate(_lines(path)):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UareError(f"{path}:{number + 1} is not JSON: {exc}") from exc
        if not isinstance(record, dict) or "index" not in record or "value" not in record:
            raise UareError(f"{path}:{number + 1} lacks index/value")
        rows.append((str(record["index"]), str(record["value"])))
    return rows


def search_rows(rows: list[tuple[str, str]], pattern: str, context: int, limit: int) -> list[Hit]:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise UareError(f"invalid regex {pattern!r}: {exc}") from exc
    if context < 0 or limit < 1:
        raise UareError(f"context must be >= 0 and limit >= 1, got {context} / {limit}")
    matches = [i for i, (_key, value) in enumerate(rows) if regex.search(value)][:limit]
    match_set = set(matches)
    shown: list[int] = sorted({j for i in matches for j in range(max(0, i - context), min(len(rows), i + context + 1))})
    return [Hit(i, rows[i][0], rows[i][1], i in match_set) for i in shown]


def search_identifiers(analysis_dir: Path, pattern: str, context: int, limit: int) -> list[Hit]:
    return search_rows(_identifier_rows(analysis_dir), pattern, context, limit)


def search_literals(analysis_dir: Path, pattern: str, context: int, limit: int) -> list[Hit]:
    return search_rows(_literal_rows(analysis_dir), pattern, context, limit)


def display_value(value: str) -> str:
    """Game-controlled text made safe for one output line: control characters escaped, length capped."""
    escaped = "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in value)
    return escaped if len(escaped) <= MAX_DISPLAY else escaped[:MAX_DISPLAY] + f"...(+{len(escaped) - MAX_DISPLAY})"


def format_hits(hits: list[Hit]) -> list[str]:
    lines: list[str] = []
    previous = None
    for hit in hits:
        if previous is not None and hit.line != previous + 1:
            lines.append("--")
        lines.append(f"{'>' if hit.is_match else ' '} {hit.key}\t{display_value(hit.value)}")
        previous = hit.line
    return lines
