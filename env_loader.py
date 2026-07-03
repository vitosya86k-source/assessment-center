"""
env_loader.py — секреты и настройки только из окружения / .env, без хардкода в коде.

get_secret("COMBO_BOT_TOKEN", files=[...]) → значение из os.environ или из .env-файлов.
Никаких токенов в исходниках (см. ревью PUF: захардкоженный токен = скомпрометирован).
"""
from __future__ import annotations

import os
from pathlib import Path


def _parse_env(path) -> dict:
    out = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def get_secret(*names: str, files=()) -> str | None:
    """Ищет значение по списку имён: сначала os.environ, потом .env-файлы."""
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    merged = {}
    for f in files:
        merged.update(_parse_env(f))
    for n in names:
        if merged.get(n):
            return merged[n]
    return None
