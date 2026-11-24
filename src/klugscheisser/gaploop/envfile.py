"""Read single keys from the live install's .env into the environment, without ever printing values."""

from __future__ import annotations

import os
from pathlib import Path

LIVE_ENV = Path.home() / "klugscheisser" / ".env"


def read_key(key: str, path: Path = LIVE_ENV) -> str | None:
    """Value of `key` from the environment, else from `path` (a dotenv file); None if missing."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :]
        if not line.startswith(f"{key}="):
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None
