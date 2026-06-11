"""信号回测：用历史日线复现技术策略，统计收益/胜率/最大回撤，并对比买入持有。

设计要点（避免「未来函数」）：
- 所有用到的指标列（compute_all 产生）均为 rolling/ewm，只依赖当日及之前数据；
- 策略在第 t 日收盘根据指标给出"目标仓位"，但在第 t+1 日收盘才成交（pos 用 shift(1)）；
- 多空仅 long-only，仓位为 0 或 1，换仓时扣交易成本（双边）。

策略只用价格/量能可复现的信号，与 app 中展示的技术信号保持一致；基本面类大师
依赖当下快照，无法点到点回放，故不纳入回测。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data
from .indicators import compute_all

_FEE = 0.0013  # 单边交易成本估计（佣金+印花税+滑点），换仓双边按 turnover 计


# ---- 各策略：返回每根 K 线的「目标仓位」Series(0/1) ----
def _pos_trend(df: pd.DataFrame) -> pd.Series:
    """趋势跟随：均线多头排列且 MACD 在多头区则持多，否则空仓。"""
    bull = (df["ma5"] > df["ma20"]) & (df["ma20"] > df["ma60"]) & (df["dif"] > df["dea"])
    return bull.astype(int)


def _pos_macd(df: pd.DataFrame) -> pd.Series:
    """MACD 金叉持多、死叉空仓（以 DIF 是否在 DEA 上方表示金叉后状态）。"""
    return (df["dif"] > df["dea"]).astype(int)


def _pos_rebound(df: pd.DataFrame) -> pd.Series:
    """超跌反弹：RSI12<=30 触发买入，回到 RSI>=55 或站上中轨后离场（状态机）。"""
    rsi = df["rsi12"].to_numpy()
    close = df["close"].to_numpy()
    mid = df["boll_mid"].to_numpy()
    pos = np.zeros(len(df), dtype=int)
    holding = 0
    for i in range(len(df)):
        if holding:
            if rsi[i] >= 55 or close[i] >= mid[i]:
                holding = 0
        else:
            if rsi[i] <= 30:
                holding = 1
        pos[i] = holding
    return pd.Series(pos, index=df.index)


def _pos_composite(df: pd.DataFrame) -> pd.Series:
    """综合信号：复现 build_analysis 的 5 项打分，verdict 偏多时持多，否则空仓。"""
    bull = pd.Series(0, index=df.index)
    bear = pd.Series(0, index=df.index)
    warn = pd.Series(0, index=df.index)

    # 趋势
    bull += ((df["ma5"] > df["ma20"]) & (df["ma20"] > df["ma60"])).astype(int)
    bear += ((df["ma5"] < df["ma20"]) & (df["ma20"] < df["ma60"])).astype(int)
    # MACD
    bull += (df["dif"] > df["dea"]).astype(int)
    bear += (df["dif"] <= df["dea"]).astype(int)
    # RSI
    warn += (df["rsi12"] >= 70).astype(int)
    bull += (df["rsi12"] <= 30).astype(int)
    # 布林
    warn += (df["close"] >= df["boll_upper"] * 0.99).astype(int)
    bull += (df["close"] <= df["boll_lower"] * 1.01).astype(int)
    # 量能（只贡献 warning）
    ratio = df["volume"] / df["vol_ma5"].replace(0, np.nan)
    warn += (ratio >= 1.5).fillna(False).astype(int)

    # 偏多即持有：看多信号占优(≥2)、无看空信号，且非高风险(warning≤1)。
    # 不照搬 build_analysis 最严格的"≥3 多且零风险"，否则几乎不触发交易，失去回测意义。
    verdict_bull = (bull >= 2) & (bear == 0) & (warn <= 1)
    return verdict_bull.astype(int)


STRATEGIES = {
    "composite": ("综合信号", "5 项技术指标综合判断为偏多时持有", _pos_composite),
    "trend": ("趋势跟随", "均线多头 + MACD 多头区才持有，顺势而为", _pos_trend),
    "macd": ("MACD 金叉", "DIF 上穿 DEA 持有，下穿离场", _pos_macd),
    "rebound": ("超跌反弹", "RSI 超卖买入，反弹至中轨/RSI 回升离场", _pos_rebound),
}


def list_strategies() -> dict:
    return {"strategies": [{"id": k, "label": v[0], "desc": v[1]} for k, v in STRATEGIES.items()]}


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1.0
    return float(dd.min())


def _trades_from_pos(pos: np.ndarray, close: np.ndarray, dates: list) -> list[dict]:
    """根据持仓序列还原每笔完整交易（建仓→平仓）。"""
    trades = []
    entry_i = None
    for i in range(len(pos)):
        if entry_i is None and pos[i] == 1:
            entry_i = i
        elif entry_i is not None and pos[i] == 0:
            ret = close[i] / close[entry_i] - 1.0
            trades.append({
                "entry_date": dates[entry_i], "entry_price": round(float(close[entry_i]), 2),
                "exit_date": dates[i], "exit_price": round(float(close[i]), 2),
                "ret": round(ret * 100, 2), "bars": i - entry_i,
            })
            entry_i = None
    if entry_i is not None:  # 末尾仍持仓，按最后收盘计为浮动盈亏
        i = len(pos) - 1
        ret = close[i] / close[entry_i] - 1.0
        trades.append({
            "entry_date": dates[entry_i], "entry_price": round(float(close[entry_i]), 2),
            "exit_date": dates[i] + "(持仓中)", "exit_price": round(float(close[i]), 2),
            "ret": round(ret * 100, 2), "bars": i - entry_i, "open": True,
        })
    return trades


def run(code: str, strategy: str = "composite", days: int = 250, adjust: str = "qfq") -> dict:
    code = str(code).strip().zfill(6)
    if strategy not in STRATEGIES:
        strategy = "composite"
    label, desc, fn = STRATEGIES[strategy]

    df, is_demo = data.get_kline(code, days=days, adjust=adjust)
    if df is None or df.empty or len(df) < 30:
        return {"code": code, "strategy": strategy, "error": "历史数据不足，无法回测", "is_demo": True}

    df = compute_all(df).reset_index(drop=True)
    dates = [str(d) for d in df["date"].tolist()]
    close = df["close"].to_numpy(dtype=float)

    desired = fn(df).to_numpy(dtype=int)
    # t 日决策、t+1 日成交：持有仓位用上一日目标仓位
    held = np.concatenate([[0], desired[:-1]])

    ret = close[1:] / close[:-1] - 1.0          # 第 1..n-1 日的收益
    pos_ret = held[1:] * ret                    # 持仓贡献的收益
    cost = np.abs(np.diff(held)) * _FEE         # 换仓成本，与 pos_ret 同长(n-1)
    net_ret = pos_ret - cost

    equity = np.cumprod(1.0 + net_ret)
    equity = np.concatenate([[1.0], equity])    # 对齐 n 个点（第 0 日=1）
    bench = close / close[0]

    n = len(df)
    total = float(equity[-1] - 1.0)
    bench_total = float(bench[-1] - 1.0)
    years = max(n / 252.0, 1e-6)
    annualized = float((equity[-1]) ** (1.0 / years) - 1.0) if equity[-1] > 0 else -1.0
    mdd = _max_drawdown(equity)
    exposure = float(held.mean())
    daily = net_ret
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 1e-9 else 0.0

    trades = _trades_from_pos(held, close, dates)
    closed = [t for t in trades if not t.get("open")]
    wins = [t for t in closed if t["ret"] > 0]
    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else None

    # 资金曲线下采样（最多 ~180 点，前端绘图够用）
    step = max(1, n // 180)
    curve = [{"date": dates[i], "eq": round(float(equity[i]), 4), "bench": round(float(bench[i]), 4)}
             for i in range(0, n, step)]
    if curve[-1]["date"] != dates[-1]:
        curve.append({"date": dates[-1], "eq": round(float(equity[-1]), 4),
                      "bench": round(float(bench[-1]), 4)})

    return {
        "code": code, "name": data.name_for(code),
        "strategy": strategy, "strategy_label": label, "strategy_desc": desc,
        "days": n, "start": dates[0], "end": dates[-1], "is_demo": is_demo,
        "fee_per_side": _FEE,
        "metrics": {
            "total_return": round(total * 100, 2),
            "annualized": round(annualized * 100, 2),
            "benchmark_return": round(bench_total * 100, 2),
            "excess": round((total - bench_total) * 100, 2),
            "max_drawdown": round(mdd * 100, 2),
            "sharpe": round(sharpe, 2),
            "win_rate": win_rate,
            "trades": len(closed),
            "exposure": round(exposure * 100, 1),
        },
        "curve": curve,
        "trades": trades[-12:],  # 最近若干笔
    }
