"""自选股本地存储：简单 JSON 文件，无需数据库。"""
from __future__ import annotations

import json
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
_WATCHLIST = _DATA_DIR / "watchlist.json"

_DEFAULT = ["600519", "000001", "300750"]


def load() -> list[str]:
    if not _WATCHLIST.exists():
        save(_DEFAULT)
        return list(_DEFAULT)
    try:
        return json.loads(_WATCHLIST.read_text(encoding="utf-8"))
    except Exception:
        return list(_DEFAULT)


def save(codes: list[str]) -> None:
    _WATCHLIST.write_text(json.dumps(codes, ensure_ascii=False, indent=2), encoding="utf-8")


def add(code: str) -> list[str]:
    code = str(code).strip().zfill(6)
    codes = load()
    if code not in codes:
        codes.append(code)
        save(codes)
    return codes


def remove(code: str) -> list[str]:
    code = str(code).strip().zfill(6)
    codes = [c for c in load() if c != code]
    save(codes)
    return codes
