"""情报层（Phase 1）：板块热度榜 + 财经快讯事件流 + 个股相关新闻。

定位：事件驱动的『主题/板块研判』，非秒级抢快讯。本阶段为纯数据 + 规则映射，
几乎不消耗 LLM token（板块标签用行业板块名做关键词匹配）。

所有 akshare 调用复用 data._ak（单线程串行，防 mini_racer/V8 跨线程崩溃）。
"""
from __future__ import annotations

import re
import time

import pandas as pd

from .data import AKSHARE_AVAILABLE, _ak, cache_get, cache_set

if AKSHARE_AVAILABLE:
    import akshare as ak


def _ak_retry(fn, *args, attempts: int = 3, **kwargs):
    """带重试的 akshare 调用：东财板块/新闻接口偶发 RemoteDisconnected。"""
    last = None
    for i in range(attempts):
        try:
            return _ak(fn, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5 * (i + 1))
    raise last


def _f(v) -> float | None:
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


# ---- 板块热度榜 ----
def _board_rows(df: pd.DataFrame, top: int) -> tuple[list, list]:
    ren = {"板块名称": "name", "板块代码": "code", "涨跌幅": "pct", "总市值": "mktcap",
           "换手率": "turnover", "上涨家数": "up", "下跌家数": "down",
           "领涨股票": "leader", "领涨股票-涨跌幅": "leader_pct"}
    df = df.rename(columns=ren)
    df["pct"] = pd.to_numeric(df["pct"], errors="coerce")
    df = df.dropna(subset=["pct"]).sort_values("pct", ascending=False)

    def pack(rows):
        out = []
        for _, r in rows.iterrows():
            out.append({
                "name": str(r.get("name", "")), "code": str(r.get("code", "")),
                "pct": round(float(r["pct"]), 2),
                "up": int(_f(r.get("up")) or 0), "down": int(_f(r.get("down")) or 0),
                "turnover": _f(r.get("turnover")),
                "leader": str(r.get("leader", "")),
                "leader_pct": _f(r.get("leader_pct")),
            })
        return out

    return pack(df.head(top)), pack(df.tail(top).iloc[::-1])


def get_sector_board(kind: str = "industry", top: int = 12) -> dict:
    """行业/概念板块热度榜：领涨 leaders + 领跌 laggards。"""
    kind = "concept" if kind == "concept" else "industry"
    ckey = f"sectors:{kind}:{top}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached
    if not AKSHARE_AVAILABLE:
        return {"kind": kind, "leaders": [], "laggards": [], "is_demo": True}
    fn = ak.stock_board_concept_name_em if kind == "concept" else ak.stock_board_industry_name_em
    try:
        df = _ak_retry(fn)
        leaders, laggards = _board_rows(df, top)
        out = {"kind": kind, "leaders": leaders, "laggards": laggards, "is_demo": False}
        cache_set(ckey, out)  # 只缓存成功结果，失败不缓存以便重试
        return out
    except Exception as exc:
        return {"kind": kind, "leaders": [], "laggards": [], "is_demo": True, "error": str(exc)[:120]}


# ---- 新闻→板块 规则标签（零 token）----
def _sector_names() -> list[str]:
    """行业板块名词典（用于把新闻打上板块标签）；只保留长度≥2，避免单字误命中。"""
    cached = cache_get("sector_names")
    if cached is not None:
        return cached
    names: list[str] = []
    if AKSHARE_AVAILABLE:
        try:
            df = _ak_retry(ak.stock_board_industry_name_em)
            names = [str(n) for n in df["板块名称"].tolist() if len(str(n)) >= 2]
        except Exception:
            names = []
    cache_set("sector_names", names)
    return names


def _tag_sectors(text: str, names: list[str]) -> list[str]:
    hit = [n for n in names if n in text]
    # 去重并最多保留 3 个，长名优先（更具体）
    hit = sorted(set(hit), key=lambda x: -len(x))
    return hit[:3]


def _clip(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


# ---- 财经快讯事件流 ----
def get_global_news(limit: int = 40) -> dict:
    """全球财经快讯（东财），并用行业板块名做规则标签。"""
    ckey = f"news_global:{limit}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached
    if not AKSHARE_AVAILABLE:
        return {"items": [], "is_demo": True}
    try:
        df = _ak_retry(ak.stock_info_global_em)
        names = _sector_names()
        items = []
        for _, r in df.head(limit).iterrows():
            title = str(r.get("标题", ""))
            summary = _clip(r.get("摘要", ""))
            items.append({
                "title": title, "summary": summary,
                "time": str(r.get("发布时间", "")), "url": str(r.get("链接", "")),
                "sectors": _tag_sectors(title + " " + summary, names),
            })
        out = {"items": items, "is_demo": False}
        cache_set(ckey, out)
        return out
    except Exception as exc:
        return {"items": [], "is_demo": True, "error": str(exc)[:120]}


# ---- 个股相关新闻 ----
def get_stock_news(code: str, limit: int = 10) -> dict:
    code = str(code).strip().zfill(6)
    ckey = f"news_stock:{code}:{limit}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached
    if not AKSHARE_AVAILABLE:
        return {"code": code, "items": [], "is_demo": True}
    try:
        df = _ak_retry(ak.stock_news_em, symbol=code)
        items = []
        for _, r in df.head(limit).iterrows():
            items.append({
                "title": str(r.get("新闻标题", "")),
                "summary": _clip(r.get("新闻内容", "")),
                "time": str(r.get("发布时间", "")),
                "source": str(r.get("文章来源", "")),
                "url": str(r.get("新闻链接", "")),
            })
        out = {"code": code, "items": items, "is_demo": False}
        cache_set(ckey, out)
        return out
    except Exception as exc:
        return {"code": code, "items": [], "is_demo": True, "error": str(exc)[:120]}
