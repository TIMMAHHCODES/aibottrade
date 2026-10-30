"""Reading and writing `.env` without destroying the file's comments.

The dashboard edits settings, and the worst possible outcome there would be
silently wiping the hand-written comments someone left for themselves. So
`.env` is updated line by line rather than regenerated.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# KEY=value, tolerant of leading whitespace and around the equals sign.
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def update_env(path: Path, changes: dict[str, str]) -> list[str]:
    """Apply `changes` to `path`, preserving all other lines and comments.

    Returns the keys actually written. Keys not already present are
    appended at the end rather than injected in the middle.
    """
    if not changes:
        return []

    lines = read_lines(path)
    seen: set[str] = set()
    out: list[str] = []

    for line in lines:
        match = _LINE.match(line)
        if match and match.group(1) in changes:
            key = match.group(1)
            out.append(f"{key}={_format(changes[key])}")
            seen.add(key)
        else:
            out.append(line)

    for key, value in changes.items():
        if key not in seen:
            out.append(f"{key}={_format(value)}")

    # Skip a stray leading blank line if we created the file.
    while out and out[0].strip() == "":
        out.pop(0)

    _write_atomic(path, "\n".join(out) + "\n")
    return list(changes)


def read_env(path: Path) -> dict[str, str]:
    """Flat key -> value view of `path`, for display. Comments dropped."""
    values: dict[str, str] = {}
    for line in read_lines(path):
        match = _LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def _format(value: str) -> str:
    """Quote only when dotenv would otherwise misread the value."""
    value = str(value)
    if value == "":
        return ""
    needs_quotes = (
        value[0] in {'"', "'", "`"}
        or value != value.strip()
        or any(ch in value for ch in " \t#\"'`")
    )
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_atomic(path: Path, text: str) -> None:
    """Write-then-rename so a crash cannot leave a truncated `.env`.

    A half-written config file would take the bot down and, worse, could
    drop PRIVATE_KEY_BASE58 entirely.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    # os.replace is atomic on Windows and POSIX.
    os.replace(tmp, path)
