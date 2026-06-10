"""技术指标计算：基于日线 OHLCV 的常用指标。

所有函数接收/返回 pandas 对象，纯计算、无网络副作用，便于单独测试。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def moving_averages(close: pd.Series, windows=(5, 10, 20, 60)) -> dict[str, pd.Series]:
    return {f"ma{w}": close.rolling(window=w, min_periods=1).mean() for w in windows}


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, pd.Series]:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2  # 国内习惯放大 2 倍显示
    return {"dif": dif, "dea": dea, "macd": hist}


def rsi(close: pd.Series, periods=(6, 12, 24)) -> dict[str, pd.Series]:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    out: dict[str, pd.Series] = {}
    for p in periods:
        avg_gain = gain.ewm(alpha=1 / p, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / p, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        out[f"rsi{p}"] = (100 - 100 / (1 + rs)).fillna(50)
    return out


def bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0) -> dict[str, pd.Series]:
    mid = close.rolling(window=window, min_periods=1).mean()
    std = close.rolling(window=window, min_periods=1).std(ddof=0)
    return {
        "boll_mid": mid,
        "boll_upper": mid + num_std * std,
        "boll_lower": mid - num_std * std,
    }


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9) -> dict[str, pd.Series]:
    low_n = low.rolling(window=n, min_periods=1).min()
    high_n = high.rolling(window=n, min_periods=1).max()
    rsv = ((close - low_n) / (high_n - low_n).replace(0, np.nan) * 100).fillna(50)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return {"kdj_k": k, "kdj_d": d, "kdj_j": j}


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """在 df（含 open/high/low/close/volume 列）上附加所有指标列。"""
    out = df.copy()
    close, high, low = out["close"], out["high"], out["low"]
    for group in (
        moving_averages(close),
        macd(close),
        rsi(close),
        bollinger(close),
        kdj(high, low, close),
    ):
        for name, series in group.items():
            out[name] = series
    out["vol_ma5"] = out["volume"].rolling(window=5, min_periods=1).mean()
    return out
