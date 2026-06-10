"""规则化「人话」信号：把指标翻译成趋势/动量/波动/量能的中文判断与风险提示。

不预测涨跌，只做信号汇总与矛盾点提示，决策权留给用户。
"""
from __future__ import annotations

import pandas as pd

# tone 取值与前端信号灯颜色对应：bullish/bearish/warning/neutral
Signal = dict


def _last(series: pd.Series, i: int = -1) -> float:
    return float(series.iloc[i])


def trend_signal(df: pd.DataFrame) -> Signal:
    ma5, ma20, ma60 = _last(df["ma5"]), _last(df["ma20"]), _last(df["ma60"])
    if ma5 > ma20 > ma60:
        return {"dim": "趋势", "tone": "bullish", "label": "多头排列",
                "text": "MA5 > MA20 > MA60，短中长期均线向上发散，趋势偏多。"}
    if ma5 < ma20 < ma60:
        return {"dim": "趋势", "tone": "bearish", "label": "空头排列",
                "text": "MA5 < MA20 < MA60，均线向下发散，趋势偏空，不宜逆势抄底。"}
    return {"dim": "趋势", "tone": "neutral", "label": "均线纠缠",
            "text": "均线相互缠绕，方向不明，属于震荡格局，追涨杀跌风险大。"}


def macd_signal(df: pd.DataFrame) -> Signal:
    dif_now, dea_now = _last(df["dif"]), _last(df["dea"])
    dif_prev, dea_prev = _last(df["dif"], -2), _last(df["dea"], -2)
    crossed_up = dif_prev <= dea_prev and dif_now > dea_now
    crossed_down = dif_prev >= dea_prev and dif_now < dea_now
    if crossed_up:
        return {"dim": "MACD", "tone": "bullish", "label": "刚金叉",
                "text": "DIF 上穿 DEA 形成金叉，动能转强（注意金叉位置高低）。"}
    if crossed_down:
        return {"dim": "MACD", "tone": "bearish", "label": "刚死叉",
                "text": "DIF 下穿 DEA 形成死叉，动能转弱。"}
    if dif_now > dea_now:
        return {"dim": "MACD", "tone": "bullish", "label": "多头区",
                "text": "DIF 在 DEA 上方运行，动能偏强但未必是新买点。"}
    return {"dim": "MACD", "tone": "bearish", "label": "空头区",
            "text": "DIF 在 DEA 下方运行，动能偏弱。"}


def rsi_signal(df: pd.DataFrame) -> Signal:
    r = _last(df["rsi12"])
    if r >= 70:
        return {"dim": "RSI", "tone": "warning", "label": f"超买 {r:.0f}",
                "text": f"RSI12={r:.0f} 进入超买区（≥70），追高风险偏大。"}
    if r <= 30:
        return {"dim": "RSI", "tone": "bullish", "label": f"超卖 {r:.0f}",
                "text": f"RSI12={r:.0f} 进入超卖区（≤30），有超跌反弹可能，但需配合趋势。"}
    return {"dim": "RSI", "tone": "neutral", "label": f"中性 {r:.0f}",
            "text": f"RSI12={r:.0f} 处于中性区间。"}


def boll_signal(df: pd.DataFrame) -> Signal:
    close = _last(df["close"])
    upper, lower, mid = _last(df["boll_upper"]), _last(df["boll_lower"]), _last(df["boll_mid"])
    if close >= upper * 0.99:
        return {"dim": "布林带", "tone": "warning", "label": "逼近上轨",
                "text": "股价贴近布林带上轨，短期处于高波动/高位区域。"}
    if close <= lower * 1.01:
        return {"dim": "布林带", "tone": "bullish", "label": "逼近下轨",
                "text": "股价贴近布林带下轨，处于相对低位，关注是否企稳。"}
    side = "中轨上方" if close >= mid else "中轨下方"
    return {"dim": "布林带", "tone": "neutral", "label": side,
            "text": f"股价位于布林带{side}，波动正常。"}


def volume_signal(df: pd.DataFrame) -> Signal:
    vol, vma5 = _last(df["volume"]), _last(df["vol_ma5"])
    ratio = vol / vma5 if vma5 else 1.0
    if ratio >= 1.5:
        return {"dim": "量能", "tone": "warning", "label": f"放量 {ratio:.1f}x",
                "text": f"当日成交量是 5 日均量的 {ratio:.1f} 倍，明显放量，需结合价格判断是进是出。"}
    if ratio <= 0.6:
        return {"dim": "量能", "tone": "neutral", "label": f"缩量 {ratio:.1f}x",
                "text": f"当日成交量仅为 5 日均量的 {ratio:.1f} 倍，缩量，参与意愿不强。"}
    return {"dim": "量能", "tone": "neutral", "label": "量能正常",
            "text": "成交量与近期均量相当。"}


def _tf_trend(close: pd.Series, short_w: int, long_w: int) -> tuple[str, str]:
    """单一周期趋势：综合「价 vs 长均线」「短均线 vs 长均线」「长均线斜率」。"""
    close = close.dropna()
    if len(close) < short_w + 1:
        return ("数据不足", "neutral")
    ms = close.rolling(short_w, min_periods=1).mean()
    ml = close.rolling(long_w, min_periods=1).mean()
    c, s, l = close.iloc[-1], ms.iloc[-1], ml.iloc[-1]
    back = min(len(ml) - 1, 4)
    slope = ml.iloc[-1] - ml.iloc[-1 - back]  # 长均线近端斜率
    if c > l and s >= l and slope >= 0:
        return ("上行", "bullish")
    if c < l and s <= l and slope <= 0:
        return ("下行", "bearish")
    return ("震荡", "neutral")


def trend_overview(df: pd.DataFrame) -> dict:
    """多周期趋势：用日线重采样出周/月线，看大方向是否共振。"""
    d = df[["date", "close"]].copy()
    d["dt"] = pd.to_datetime(d["date"])
    close = d.set_index("dt")["close"]
    weekly = close.resample("W").last().dropna()
    monthly = close.resample("ME").last().dropna()

    tfs = {
        "daily": ("日线", *_tf_trend(close, 20, 60)),
        "weekly": ("周线", *_tf_trend(weekly, 5, 20)),
        "monthly": ("月线", *_tf_trend(monthly, 3, 12)),
    }
    items = {k: {"name": v[0], "label": v[1], "tone": v[2]} for k, v in tfs.items()}
    tones = [v[2] for v in tfs.values() if v[1] != "数据不足"]
    if tones and all(t == "bullish" for t in tones):
        align = {"label": "多周期共振向上", "tone": "bullish"}
    elif tones and all(t == "bearish" for t in tones):
        align = {"label": "多周期共振向下", "tone": "bearish"}
    elif "bullish" in tones and "bearish" in tones:
        align = {"label": "周期方向背离（大小级别不一致）", "tone": "warning"}
    else:
        align = {"label": "方向不明 / 震荡", "tone": "neutral"}
    return {"items": items, "align": align}


def build_analysis(df: pd.DataFrame) -> dict:
    signals = [
        trend_signal(df), macd_signal(df), rsi_signal(df),
        boll_signal(df), volume_signal(df),
    ]
    bull = sum(1 for s in signals if s["tone"] == "bullish")
    bear = sum(1 for s in signals if s["tone"] == "bearish")
    warn = sum(1 for s in signals if s["tone"] == "warning")

    if bull >= 3 and warn == 0:
        verdict, tone = "偏多（信号较一致）", "bullish"
    elif bear >= 3:
        verdict, tone = "偏空（趋势走弱）", "bearish"
    elif warn >= 2 or (bull >= 1 and warn >= 1):
        verdict, tone = "信号矛盾 / 高位震荡（谨慎）", "warning"
    else:
        verdict, tone = "中性震荡（方向不明）", "neutral"

    summary = (
        f"综合 5 项指标：偏多 {bull}、偏空 {bear}、风险提示 {warn}。"
        f"当前判断为「{verdict}」。指标互相矛盾时不是低风险位置，"
        "请结合基本面与自身计划，先用模拟盘或小资金验证。"
    )
    return {
        "verdict": verdict,
        "verdict_tone": tone,
        "counts": {"bullish": bull, "bearish": bear, "warning": warn},
        "signals": signals,
        "summary": summary,
        "disclaimer": "本分析为规则化信号汇总，不构成投资建议；指标存在滞后性，决策与风险由你自行承担。",
    }
