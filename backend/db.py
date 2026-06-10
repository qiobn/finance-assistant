"""SQLite 持久层：把 K 线 / 基本面落盘，作为页面读取的"源"。

akshare 只负责增量补数；页面读本地库 → 毫秒级、重启不丢、TTL 过期不再全量冷拉。
- WAL 模式：支持并发读 + 单写，配合连接级 timeout 等待写锁。
- 每次操作开一个短连接（sqlite3.connect 很轻），避免跨线程共享同一连接。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
_DB_PATH = _DATA_DIR / "cache.db"

_INITED = False

# ---- 数据清理参数（防止 cache.db 无限增长）----
RETENTION_DAYS = 450   # 每只股票最多保留多少天 K 线
MAX_CODES = 400        # 最多保留多少只股票（按访问时间 LRU 淘汰，自选股受保护）
_CLEANUP_INTERVAL = 3600  # 清理最小间隔（秒）
_ACCESS_THROTTLE = 3600   # 同一只股票访问标记最多 1 小时写一次，避免读路径频繁写

_access_seen: dict[tuple[str, str], float] = {}
_last_cleanup = 0.0


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=15, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    global _INITED
    if _INITED:
        return
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kline (
                code   TEXT NOT NULL,
                adjust TEXT NOT NULL,
                date   TEXT NOT NULL,
                open   REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (code, adjust, date)
            );
            CREATE TABLE IF NOT EXISTS fundamentals (
                code         TEXT PRIMARY KEY,
                payload_json TEXT,
                updated_at   REAL
            );
            CREATE TABLE IF NOT EXISTS stock_meta (
                code TEXT PRIMARY KEY, name TEXT, updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS kline_access (
                code TEXT PRIMARY KEY, last_access REAL
            );
            """
        )
    _INITED = True


# ---- K 线 ----
def _touch_access(code: str, conn: sqlite3.Connection | None = None) -> None:
    """记录访问时间（用于 LRU 淘汰），同一 code 1 小时内只写一次。"""
    now = time.time()
    if now - _access_seen.get(code, 0) < _ACCESS_THROTTLE:
        return
    _access_seen[code] = now
    try:
        if conn is not None:
            conn.execute(
                "INSERT OR REPLACE INTO kline_access (code, last_access) VALUES (?,?)",
                (code, now),
            )
        else:
            with _conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO kline_access (code, last_access) VALUES (?,?)",
                    (code, now),
                )
    except Exception:
        pass


def get_kline_df(code: str, adjust: str) -> pd.DataFrame:
    """返回该 code/adjust 已存的全部 K 线，按日期升序。空则返回空 DataFrame。"""
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM kline "
            "WHERE code=? AND adjust=? ORDER BY date ASC",
            (code, adjust),
        ).fetchall()
    _touch_access(code)
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def last_kline_date(code: str, adjust: str) -> str | None:
    init_db()
    with _conn() as conn:
        r = conn.execute(
            "SELECT MAX(date) FROM kline WHERE code=? AND adjust=?", (code, adjust)
        ).fetchone()
    return r[0] if r and r[0] else None


def upsert_kline(code: str, adjust: str, df: pd.DataFrame) -> int:
    """写入/覆盖若干日 K 线。df 列：date/open/high/low/close/volume。返回写入行数。"""
    if df is None or df.empty:
        return 0
    init_db()
    rows = [
        (code, adjust, str(r["date"]),
         float(r["open"]), float(r["high"]), float(r["low"]),
         float(r["close"]), float(r["volume"]))
        for _, r in df.iterrows()
    ]
    with _conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO kline "
            "(code, adjust, date, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


# ---- 基本面 ----
def get_fundamentals(code: str, max_age_seconds: float = 86400) -> dict | None:
    """读取基本面缓存；超过 max_age 视为过期返回 None。"""
    init_db()
    with _conn() as conn:
        r = conn.execute(
            "SELECT payload_json, updated_at FROM fundamentals WHERE code=?", (code,)
        ).fetchone()
    if not r:
        return None
    payload_json, updated_at = r
    if updated_at and (time.time() - updated_at) > max_age_seconds:
        return None
    try:
        return json.loads(payload_json)
    except Exception:
        return None


def upsert_fundamentals(code: str, payload: dict) -> None:
    init_db()
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fundamentals (code, payload_json, updated_at) "
            "VALUES (?,?,?)",
            (code, json.dumps(payload, ensure_ascii=False), time.time()),
        )


# ---- 数据清理：保留窗口 + LRU 淘汰，避免库无限增长 ----
def cleanup(protect: set[str] | None = None,
            retention_days: int = RETENTION_DAYS, max_codes: int = MAX_CODES) -> dict:
    """1) 删除超过保留窗口的旧 K 线；2) 股票数超过上限时按最近访问淘汰最旧的（自选股受保护）。"""
    init_db()
    protect = {str(c).zfill(6) for c in (protect or set())}
    cutoff = (dt.date.today() - dt.timedelta(days=retention_days)).strftime("%Y-%m-%d")
    stats = {"pruned_rows": 0, "evicted_codes": 0}
    evicted = False
    with _conn() as conn:
        cur = conn.execute("DELETE FROM kline WHERE date < ?", (cutoff,))
        stats["pruned_rows"] = cur.rowcount or 0

        codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM kline").fetchall()]
        if len(codes) > max_codes:
            acc = {r[0]: (r[1] or 0.0)
                   for r in conn.execute("SELECT code, last_access FROM kline_access").fetchall()}
            # 自选股排最后（最不易被淘汰）；其余按最近访问时间升序，最旧的先淘汰
            ordered = sorted(codes, key=lambda c: (c in protect, acc.get(c, 0.0)))
            evict = [c for c in ordered if c not in protect][: len(codes) - max_codes]
            for c in evict:
                conn.execute("DELETE FROM kline WHERE code=?", (c,))
                conn.execute("DELETE FROM fundamentals WHERE code=?", (c,))
                conn.execute("DELETE FROM kline_access WHERE code=?", (c,))
            stats["evicted_codes"] = len(evict)
            evicted = bool(evict)

    if stats["pruned_rows"] or evicted:  # 回收磁盘空间（VACUUM 需在事务外、自动提交连接执行）
        try:
            vac = sqlite3.connect(_DB_PATH, timeout=15, isolation_level=None)
            vac.execute("VACUUM")
            vac.close()
        except Exception:
            pass
    return stats


def maybe_cleanup(protect: set[str] | None = None) -> None:
    """节流触发清理：最小间隔 _CLEANUP_INTERVAL。失败不影响主流程。"""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    try:
        cleanup(protect=protect)
    except Exception:
        pass
