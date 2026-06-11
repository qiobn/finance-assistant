"""数据层：通过 akshare 获取 A 股数据；网络不可用时回退到本地演示数据。"""
from __future__ import annotations

import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import numpy as np
import pandas as pd

from . import db

# akshare 的部分接口（如新浪 stock_zh_a_daily）内部用 mini_racer(V8) 执行 JS，
# 而 V8 非线程安全：多线程并发调用会导致进程级崩溃。
# 因此把所有 akshare 调用收敛到「单一线程」串行执行，调用方线程阻塞等待结果。
# 这样既保留上层（DB 读 + 指标计算）的并发，又保证 V8 永远只在一个线程里运行。
_AK_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="akshare")


def _ak(fn, *args, **kwargs):
    """在专用单线程里执行 akshare 调用（防 mini_racer/V8 跨线程崩溃）。"""
    return _AK_EXEC.submit(fn, *args, **kwargs).result()

try:
    import akshare as ak

    AKSHARE_AVAILABLE = True
except Exception:  # pragma: no cover - 仅在依赖缺失时触发
    ak = None
    AKSHARE_AVAILABLE = False


# 演示用股票（离线 / 网络失败时可用），便于先把界面跑起来
DEMO_STOCKS = {
    "600519": "贵州茅台",
    "000001": "平安银行",
    "300750": "宁德时代",
}


class DataError(Exception):
    pass


# ---- 简单 TTL 内存缓存：减少重复取数与数据源限流压力 ----
_CACHE: dict[str, tuple[float, object]] = {}
_TTL_SECONDS = 300


def cache_get(key: str):
    item = _CACHE.get(key)
    if item and (time.time() - item[0] < _TTL_SECONDS):
        return item[1]
    return None


def cache_set(key: str, value) -> None:
    _CACHE[key] = (time.time(), value)


@lru_cache(maxsize=1)
def _stock_list() -> pd.DataFrame:
    """全部 A 股代码与名称，缓存于内存。"""
    if not AKSHARE_AVAILABLE:
        return pd.DataFrame(
            [{"code": c, "name": n} for c, n in DEMO_STOCKS.items()]
        )
    df = _ak(ak.stock_info_a_code_name)
    df = df.rename(columns={"code": "code", "name": "name"})
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df[["code", "name"]]


def _is_fund(code: str) -> bool:
    """ETF / LOF 等场内基金代码：SH 5xxxxx、SZ 15xxxx/16xxxx（个股不会以这些开头）。"""
    code = str(code).strip().zfill(6)
    return code.startswith("5") or code[:2] in ("15", "16")


@lru_cache(maxsize=1)
def _fund_list() -> pd.DataFrame:
    """场内 ETF 代码与名称，缓存于内存（best-effort，失败返回空表）。"""
    if not AKSHARE_AVAILABLE:
        return pd.DataFrame(columns=["code", "name"])
    try:
        df = _ak(ak.fund_etf_spot_em)
        code_col = "代码" if "代码" in df.columns else df.columns[0]
        name_col = "名称" if "名称" in df.columns else df.columns[1]
        out = df[[code_col, name_col]].rename(columns={code_col: "code", name_col: "name"})
        out["code"] = out["code"].astype(str).str.zfill(6)
        return out[["code", "name"]]
    except Exception:
        return pd.DataFrame(columns=["code", "name"])


def search_stocks(query: str, limit: int = 20) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    try:
        df = _stock_list()
    except Exception as exc:  # 列表拉取失败时退化为演示集合
        df = pd.DataFrame([{"code": c, "name": n} for c, n in DEMO_STOCKS.items()])
    funds = _fund_list()
    if not funds.empty:  # 把 ETF 一并纳入检索
        df = pd.concat([df, funds], ignore_index=True)
    mask = df["code"].str.contains(query) | df["name"].str.contains(query)
    return df[mask].head(limit).to_dict("records")


def _etf_spot_map() -> dict:
    """ETF 实时行情快照 {code: 最新价}，TTL 缓存。"""
    cached = cache_get("etf_spot_map")
    if cached is not None:
        return cached
    m: dict = {}
    if AKSHARE_AVAILABLE:
        try:
            df = _ak(ak.fund_etf_spot_em)
            df["代码"] = df["代码"].astype(str).str.zfill(6)
            m = {r["代码"]: float(r["最新价"]) for _, r in df.iterrows()
                 if pd.notna(r.get("最新价"))}
        except Exception:
            m = {}
    cache_set("etf_spot_map", m)
    return m


def latest_price(code: str) -> float | None:
    """实时最新价（盘中现价 / 收盘后当日收盘）；取不到返回 None。

    仅 ETF/LOF 需要：其日线源（新浪）当日收盘有滞后，故用实时快照补当日价。
    个股日线源（东财）已含当日 bar，直接用最新收盘即可，无需额外实时查询。
    """
    code = str(code).strip().zfill(6)
    if not _is_fund(code):
        return None
    try:
        p = _etf_spot_map().get(code)
        return float(p) if p else None
    except Exception:
        return None


def name_for(code: str) -> str:
    code = str(code).strip().zfill(6)
    try:
        df = _stock_list()
        hit = df[df["code"] == code]
        if not hit.empty:
            return str(hit.iloc[0]["name"])
    except Exception:
        pass
    if _is_fund(code):  # ETF/LOF 名称
        try:
            funds = _fund_list()
            hit = funds[funds["code"] == code]
            if not hit.empty:
                return str(hit.iloc[0]["name"])
        except Exception:
            pass
    return DEMO_STOCKS.get(code, code)


def _demo_kline(code: str, days: int) -> pd.DataFrame:
    """生成确定性的随机游走演示数据（同一 code 结果稳定）。"""
    rng = np.random.default_rng(abs(hash(code)) % (2**32))
    n = days
    start_price = 50 + (abs(hash(code)) % 150)
    returns = rng.normal(0.0005, 0.02, n)
    close = start_price * np.cumprod(1 + returns)
    open_ = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
    volume = rng.integers(5_000_000, 80_000_000, n)
    end = dt.date.today()
    dates = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "open": open_.round(2),
            "high": high.round(2),
            "low": low.round(2),
            "close": close.round(2),
            "volume": volume,
        }
    )


def _sina_symbol(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名/类型，按日期升序；不做截断（全量入库）。"""
    df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().sort_values("date").reset_index(drop=True)


def _eastmoney_range(code: str, start: dt.date, end: dt.date, adjust: str) -> pd.DataFrame | None:
    for attempt in range(3):  # 数据源偶发断连，重试几次
        try:
            raw = _ak(
                ak.stock_zh_a_hist,
                symbol=code, period="daily",
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
                adjust=adjust,
            )
            if raw is not None and not raw.empty:
                return _normalize(
                    raw.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                                        "最低": "low", "收盘": "close", "成交量": "volume"})
                )
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    return None


def _etf_range(code: str, start: dt.date, end: dt.date, adjust: str) -> pd.DataFrame | None:
    """场内基金(ETF/LOF)日线：主源新浪（稳定、全量后过滤），备源东财。"""
    # 场内基金交易所前缀：SH 基金以 5 开头，SZ 基金以 15/16 开头
    sina_sym = ("sh" if code.startswith("5") else "sz") + code
    try:
        raw = _ak(ak.fund_etf_hist_sina, symbol=sina_sym)
        if raw is not None and not raw.empty:
            df = _normalize(raw)
            return df[df["date"] >= start.strftime("%Y-%m-%d")].reset_index(drop=True)
    except Exception:
        pass
    for attempt in range(2):
        try:
            raw = _ak(
                ak.fund_etf_hist_em,
                symbol=code, period="daily",
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
                adjust=adjust or "",
            )
            if raw is not None and not raw.empty:
                return _normalize(
                    raw.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                                        "最低": "low", "收盘": "close", "成交量": "volume"})
                )
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    return None


def _sina_full(code: str, adjust: str) -> pd.DataFrame | None:
    try:
        raw = _ak(ak.stock_zh_a_daily, symbol=_sina_symbol(code), adjust=adjust or "qfq")
        if raw is not None and not raw.empty:
            return _normalize(raw)
    except Exception:
        return None
    return None


def _fetch_kline_from_source(code: str, adjust: str, start: dt.date) -> pd.DataFrame | None:
    """主源（东财, 指定区间）→ 备源（新浪, 全量后过滤）；ETF/LOF 走基金接口。"""
    end = dt.date.today()
    if _is_fund(code):
        return _etf_range(code, start, end, adjust)
    df = _eastmoney_range(code, start, end, adjust)
    if df is None or df.empty:
        df = _sina_full(code, adjust)
        if df is not None and not df.empty:
            df = df[df["date"] >= start.strftime("%Y-%m-%d")].reset_index(drop=True)
    return df


_HISTORY_DAYS = 400  # 首次抓取的历史跨度（自然日）


def get_kline(code: str, days: int = 160, adjust: str = "qfq") -> tuple[pd.DataFrame, bool]:
    """返回 (DataFrame, is_demo)。优先读 SQLite，仅向数据源补缺失区间。

    DataFrame 列：date/open/high/low/close/volume。
    """
    code = str(code).strip().zfill(6)
    if not AKSHARE_AVAILABLE:
        return _demo_kline(code, days), True

    stored = db.get_kline_df(code, adjust)
    today = dt.date.today()
    check_key = f"klcheck:{code}:{adjust}"  # 节流：5 分钟内不重复打源

    need = stored.empty or (
        stored["date"].max() < today.strftime("%Y-%m-%d") and cache_get(check_key) is None
    )
    if need:
        if stored.empty:
            start = today - dt.timedelta(days=_HISTORY_DAYS)
        else:
            start = (pd.to_datetime(stored["date"].max()) + pd.Timedelta(days=1)).date()
        cache_set(check_key, True)  # 标记已检查（无论成功与否），避免短时间反复打源
        fresh = _fetch_kline_from_source(code, adjust, start)
        if fresh is not None and not fresh.empty:
            db.upsert_kline(code, adjust, fresh)
            stored = db.get_kline_df(code, adjust)

    if stored.empty:
        return _demo_kline(code, days), True
    return stored.tail(days).reset_index(drop=True), False


# 大盘指数总览：代码 -> 友好名称（新浪指数 spot 用 sh/sz 前缀代码）
_INDEX_TARGETS = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000300", "沪深300"),
    ("sh000016", "上证50"),
]


def _demo_index_overview() -> dict:
    rng = np.random.default_rng(int(time.time()) // _TTL_SECONDS)
    indices = []
    for code, name in _INDEX_TARGETS:
        base = 2000 + abs(hash(code)) % 2000
        pct = round(float(rng.normal(0, 1.0)), 2)
        indices.append({"code": code, "name": name,
                        "price": round(base * (1 + pct / 100), 2),
                        "change": round(base * pct / 100, 2), "pct": pct})
    return {"indices": indices,
            "breadth": {"up": 2600, "down": 2200, "flat": 200, "limit_up": 30, "limit_down": 8},
            "is_demo": True}


def get_index_overview() -> dict:
    """主要指数行情 + 全市场涨跌家数。失败回退演示数据。"""
    cached = cache_get("index_overview")
    if cached is not None:
        return cached
    if not AKSHARE_AVAILABLE:
        return _demo_index_overview()

    out: dict = {"indices": [], "breadth": {}, "is_demo": False}
    wanted = dict(_INDEX_TARGETS)
    try:
        df = _ak(ak.stock_zh_index_spot_sina)
        df["代码"] = df["代码"].astype(str)
        for code, name in _INDEX_TARGETS:
            hit = df[df["代码"] == code]
            if not hit.empty:
                r = hit.iloc[0]
                out["indices"].append({
                    "code": code, "name": name,
                    "price": round(float(r["最新价"]), 2),
                    "change": round(float(r["涨跌额"]), 2),
                    "pct": round(float(r["涨跌幅"]), 2),
                })
    except Exception:
        return _demo_index_overview()

    # 全市场涨跌家数（乐咕乐股）
    try:
        act = _ak(ak.stock_market_activity_legu)
        kv = {str(r["item"]): r["value"] for _, r in act.iterrows()}

        def _i(*keys):
            for k in keys:
                if k in kv:
                    try:
                        return int(float(kv[k]))
                    except Exception:
                        return None
            return None

        out["breadth"] = {
            "up": _i("上涨"), "down": _i("下跌"), "flat": _i("平盘"),
            "limit_up": _i("涨停"), "limit_down": _i("跌停"),
        }
    except Exception:
        out["breadth"] = {}

    if not out["indices"]:
        return _demo_index_overview()
    cache_set("index_overview", out)
    return out


def get_pool_codes(scope: str) -> list[str]:
    """股票池：返回成分股 6 位代码列表。scope: sz50 / hs300。"""
    scope = (scope or "").lower()
    cached = cache_get(f"pool:{scope}")
    if cached is not None:
        return cached
    codes: list[str] = []
    if not AKSHARE_AVAILABLE:
        return list(DEMO_STOCKS.keys())
    try:
        if scope == "sz50":
            df = _ak(ak.index_stock_cons, symbol="000016")
            col = "品种代码" if "品种代码" in df.columns else df.columns[0]
            codes = [str(c).zfill(6) for c in df[col].tolist()]
        elif scope == "hs300":
            df = _ak(ak.index_stock_cons_csindex, symbol="000300")
            col = "成分券代码" if "成分券代码" in df.columns else "品种代码"
            codes = [str(c).zfill(6) for c in df[col].tolist()]
    except Exception:
        codes = []
    if codes:
        cache_set(f"pool:{scope}", codes)
    return codes


def get_fundamentals(code: str) -> dict:
    """估值（Baidu）+ 财务摘要（同花顺）+ 资金流（东财, best-effort）。"""
    code = str(code).strip().zfill(6)
    ckey = f"fund:{code}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached
    persisted = db.get_fundamentals(code)  # 当天内有效则直接用
    if persisted is not None:
        cache_set(ckey, persisted)
        return persisted

    out: dict = {"valuation": {}, "financials": [], "fund_flow": None}
    if not AKSHARE_AVAILABLE:
        return out

    # 估值：百度源（PE/PB），取近五年序列 → 当前值 + 历史分位（越低越便宜）
    for key, ind in (("pe_ttm", "市盈率(TTM)"), ("pb", "市净率")):
        try:
            df = _ak(ak.stock_zh_valuation_baidu, symbol=code, indicator=ind, period="近五年")
            if df is not None and not df.empty:
                vals = pd.to_numeric(df["value"], errors="coerce").dropna()
                vals = vals[vals > 0]  # 估值为负（亏损）不纳入分位
                if not vals.empty:
                    cur = float(vals.iloc[-1])
                    out["valuation"][key] = round(cur, 2)
                    pct = float((vals <= cur).sum()) / len(vals) * 100
                    out["valuation"][key + "_pct"] = round(pct, 1)
                    out["valuation"][key + "_n"] = len(vals)
                    # 历史中枢（中位数）：估值回归法的锚
                    out["valuation"][key + "_median"] = round(float(vals.median()), 2)
        except Exception:
            out["valuation"][key] = None

    # 财务摘要：同花顺源（稳定），取最近 4 个报告期
    try:
        df = _ak(ak.stock_financial_abstract_ths, symbol=code, indicator="按报告期")
        cols = ["报告期", "营业总收入", "营业总收入同比增长率", "净利润", "净利润同比增长率",
                "净资产收益率", "销售毛利率", "资产负债率", "基本每股收益"]
        avail = [c for c in cols if c in df.columns]
        recent = df[avail].tail(4).iloc[::-1]
        out["financials"] = [
            {c: (None if (v is False or pd.isna(v)) else v) for c, v in row.items()}
            for row in recent.to_dict("records")
        ]
    except Exception:
        out["financials"] = []

    # 资金流：东财源（沙箱不稳，best-effort）
    try:
        market = "sh" if code.startswith("6") else ("bj" if code.startswith(("4", "8")) else "sz")
        df = _ak(ak.stock_individual_fund_flow, stock=code, market=market)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            col = next((c for c in df.columns if "主力净流入-净额" in c), None)
            if col:
                out["fund_flow"] = {"date": str(last.get("日期", "")), "main_net": round(float(last[col]) / 1e4, 1)}  # 万元
    except Exception:
        out["fund_flow"] = None

    db.upsert_fundamentals(code, out)
    cache_set(ckey, out)
    return out
