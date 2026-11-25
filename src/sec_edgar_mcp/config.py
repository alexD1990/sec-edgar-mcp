"""Load settings from a .env file so the server works when started by an MCP client
that cannot pass environment variables.

Order (first wins): real environment variables, then .env in the current directory,
then .env in the project root (next to pyproject.toml).
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def load_env(search: list[Path] | None = None) -> list[Path]:
    """Fill missing environment variables from .env files. Returns the files that were read."""
    candidates = search if search is not None else [Path.cwd() / ".env", PROJECT_ROOT / ".env"]
    used: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        path = path.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        used.append(path)
        for key, value in parse_env_file(path).items():
            os.environ.setdefault(key, value)
    return used
