"""投资大师 Skills：把行情/指标/估值翻译成「可执行的买卖纪律」。

每位大师 = 一套规则，输出：当前该做什么(action) + 买入区间(buy_zone)
+ 止盈位/条件(take_profit) + 止损位(stop_loss) + 依据(reason)。

重要边界：这些是**规则化研究信号**，不是投资建议，更不是「保证买卖点」。
数据为日线级别、非实时；价位为参考区间，实际下单以你的行情软件为准。
大师不预测「几月几号买」，而是给「在什么位置、满足什么条件就买/卖」。
"""
from __future__ import annotations

import pandas as pd


def _f(v) -> float | None:
    """尽量把各种类型（含 '12.3%' 字符串）转成 float。"""
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.replace("%", "").replace(",", "").strip()
            if not v or v in {"-", "—", "False"}:
                return None
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _last(df: pd.DataFrame, col: str) -> float | None:
    if col not in df.columns:
        return None
    return _f(df[col].iloc[-1])


def _round(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(float(x), n)


def _zone(lo: float | None, hi: float | None) -> list | None:
    if lo is None or hi is None:
        return None
    lo, hi = min(lo, hi), max(lo, hi)
    return [round(lo, 2), round(hi, 2)]


def _key_levels(df: pd.DataFrame) -> dict:
    """关键价位：近端支撑/压力 + 均线 + 近 N 日高低点。"""
    close = _last(df, "close")
    ma20 = _last(df, "ma20")
    ma60 = _last(df, "ma60")
    bu = _last(df, "boll_upper")
    bl = _last(df, "boll_lower")
    win = df.tail(min(60, len(df)))
    recent_low = _f(win["low"].min()) if "low" in win else None
    recent_high = _f(win["high"].max()) if "high" in win else None

    # 近端支撑：低于现价的候选里取「最高」的那个（离现价最近的支撑）
    below = [x for x in (ma20, ma60, bl, recent_low) if x is not None and close and x < close]
    above = [x for x in (ma20, bu, recent_high) if x is not None and close and x > close]
    support = max(below) if below else (recent_low if recent_low is not None else None)
    resistance = min(above) if above else (recent_high if recent_high is not None else None)
    return {
        "close": _round(close), "ma20": _round(ma20), "ma60": _round(ma60),
        "boll_upper": _round(bu), "boll_lower": _round(bl),
        "recent_low": _round(recent_low), "recent_high": _round(recent_high),
        "support": _round(support), "resistance": _round(resistance),
    }


def _macd_state(df: pd.DataFrame) -> str:
    if len(df) < 2 or "dif" not in df.columns:
        return "na"
    dif, dea = _last(df, "dif"), _last(df, "dea")
    dif_p, dea_p = _f(df["dif"].iloc[-2]), _f(df["dea"].iloc[-2])
    if None in (dif, dea, dif_p, dea_p):
        return "na"
    if dif_p <= dea_p and dif > dea:
        return "golden"
    if dif_p >= dea_p and dif < dea:
        return "dead"
    return "above" if dif > dea else "below"


# ---- 各大师 Skill：返回 None 表示该 skill 当前未触发/数据不足时仍会给说明 ----

def _trend_master(df: pd.DataFrame, trends: dict, lv: dict) -> dict:
    """趋势跟随（顺势派）：只在大方向(周/月)向上时顺势买回踩。"""
    items = (trends or {}).get("items") or {}
    wk = (items.get("weekly") or {}).get("tone")
    mo = (items.get("monthly") or {}).get("tone")
    day = (items.get("daily") or {}).get("tone")
    close, ma20, ma60 = lv["close"], lv["ma20"], lv["ma60"]
    rsi = _last(df, "rsi12")
    macd = _macd_state(df)
    up_big = (wk == "bullish") or (mo == "bullish")
    down_big = (wk == "bearish") and (mo == "bearish")

    base = {"key": "trend", "name": "趋势跟随（顺势派）", "horizon": "中短线"}
    if down_big or (wk == "bearish" and mo != "bullish"):
        return {**base, "action": "回避 / 不逆势抄底", "tone": "bearish",
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": "周/月线大方向向下，顺势纪律是不买、空仓等右侧信号；逆势抄底胜率低。"}
    if not up_big:
        return {**base, "action": "观望（趋势未确认）", "tone": "neutral",
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": "周/月线方向不明，等大级别转多或日线放量突破再跟。"}

    buy_zone = _zone(ma20 * 0.97, ma20 * 1.03) if ma20 else None
    take_profit = lv["boll_upper"]
    stop = _round(min(x for x in (ma60, close * 0.92 if close else None) if x is not None)) \
        if (ma60 or close) else None
    if rsi is not None and rsi >= 75:
        action, tone = "减仓 / 不追高", "warning"
        reason = (f"大方向向上但 RSI={rsi:.0f} 超买、贴近上轨，"
                  f"顺势者在此减仓锁利，等回踩 MA20({ma20}) 再接。")
    elif close and ma20 and abs(close / ma20 - 1) <= 0.03:
        action, tone = "逢回踩分批买入", "bullish"
        reason = (f"周/月线向上，价回踩到 MA20({ma20}) 附近企稳，是顺势买点；"
                  f"买入区≈{buy_zone}，跌破 MA60({ma60}) 或 −8% 离场。")
    elif close and ma20 and close > ma20 * 1.03:
        action, tone = "持有 / 等回踩", "neutral"
        reason = (f"趋势向上但已偏离 MA20({ma20})，追高性价比低；"
                  f"持有者拿住，新仓等回踩到 {buy_zone} 再进。")
    else:
        action, tone = "趋势偏多，控制仓位", "bullish"
        reason = f"大方向向上，{('MACD金叉、' if macd == 'golden' else '')}回踩 MA20({ma20}) 是相对安全的买点。"
    return {**base, "action": action, "tone": tone, "buy_zone": buy_zone,
            "take_profit": take_profit, "stop_loss": stop, "reason": reason}


def _value_master(df: pd.DataFrame, funda: dict | None, lv: dict) -> dict:
    """价值/逆向（格雷厄姆·巴菲特·Burry）：用近五年估值分位判断便宜与否。"""
    base = {"key": "value", "name": "价值 / 逆向（格雷厄姆·巴菲特）", "horizon": "中长线"}
    v = (funda or {}).get("valuation") or {}
    pe, pe_pct = _f(v.get("pe_ttm")), _f(v.get("pe_ttm_pct"))
    pb, pb_pct = _f(v.get("pb")), _f(v.get("pb_pct"))
    pcts = [p for p in (pe_pct, pb_pct) if p is not None]
    if not pcts:
        return {**base, "action": "数据不足", "tone": "neutral", "buy_zone": None,
                "take_profit": None, "stop_loss": None,
                "reason": "暂无近五年 PE/PB 分位数据，价值视角无法判断便宜与否（亏损股估值也会失真）。"}
    low = min(pcts)
    close, recent_low = lv["close"], lv["recent_low"]
    tag = " / ".join(filter(None, [
        f"PE分位{pe_pct:.0f}%" if pe_pct is not None else None,
        f"PB分位{pb_pct:.0f}%" if pb_pct is not None else None,
    ]))
    if low <= 20:
        buy_zone = _zone(recent_low, close)
        return {**base, "action": "估值很便宜，积极分批", "tone": "bullish",
                "buy_zone": buy_zone, "take_profit": None,
                "stop_loss": _round(close * 0.85) if close else None,
                "reason": (f"{tag}，处于近五年低位区（安全边际较好）。价值派分批买、越跌越买；"
                           f"估值修复到贵区(>70%分位)再考虑减；止损放宽到约 −15%（防基本面恶化）。")}
    if low <= 30:
        return {**base, "action": "偏便宜，可小批建仓", "tone": "bullish",
                "buy_zone": _zone(recent_low, close), "take_profit": None,
                "stop_loss": _round(close * 0.85) if close else None,
                "reason": f"{tag}，估值偏低。可小批建仓，留子弹等更低；分位回到 70%+ 时分批减。"}
    if low >= 70:
        return {**base, "action": "估值偏贵，不买 / 可减", "tone": "bearish",
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": f"{tag}，处于近五年高位区，缺乏安全边际；价值派此时不追，持仓者考虑分批兑现。"}
    return {**base, "action": "估值中性，耐心等", "tone": "neutral",
            "buy_zone": None, "take_profit": None, "stop_loss": None,
            "reason": f"{tag}，估值不算便宜也不算贵；价值派偏好等更明显的低估再出手。"}


def _rebound_master(df: pd.DataFrame, lv: dict) -> dict:
    """超跌反弹（短线博弈）：RSI 超卖 + 贴近布林下轨时博反弹，仓位要小、止损要严。"""
    base = {"key": "rebound", "name": "超跌反弹（短线）", "horizon": "短线"}
    rsi = _last(df, "rsi12")
    close, bl, bmid, ma20 = lv["close"], lv["boll_lower"], _last(df, "boll_mid"), lv["ma20"]
    near_lower = close is not None and bl is not None and close <= bl * 1.02
    if rsi is not None and rsi <= 30 and near_lower:
        buy_zone = _zone(bl * 0.98, bl * 1.03) if bl else None
        tp = _round(bmid if bmid else ma20)
        stop = _round(lv["recent_low"] * 0.96) if lv["recent_low"] else None
        return {**base, "action": "博反弹（小仓位）", "tone": "bullish",
                "buy_zone": buy_zone, "take_profit": tp, "stop_loss": stop,
                "reason": (f"RSI={rsi:.0f} 超卖且贴近布林下轨({bl})，有超跌反弹机会；"
                           f"目标看中轨/MA20({tp})，跌破近期低点约 −4% 必须止损。高风险、仓位要小。")}
    why = []
    if rsi is not None:
        why.append(f"RSI={rsi:.0f}{'（未超卖）' if rsi > 30 else ''}")
    if not near_lower:
        why.append("未贴近布林下轨")
    return {**base, "action": "未触发", "tone": "neutral", "buy_zone": None,
            "take_profit": None, "stop_loss": None,
            "reason": "当前不在超跌区（" + "，".join(why or ["条件不足"]) + "），无反弹买点。"}


def _growth_master(df: pd.DataFrame, funda: dict | None, lv: dict) -> dict:
    """成长匹配（彼得林奇·PEG）：成长与估值是否匹配，PEG<1 偏低。"""
    base = {"key": "growth", "name": "成长匹配（彼得林奇·PEG）", "horizon": "中长线"}
    v = (funda or {}).get("valuation") or {}
    pe = _f(v.get("pe_ttm"))
    growth = None
    for row in ((funda or {}).get("financials") or []):
        g = _f(row.get("净利润同比增长率"))
        if g is not None:
            growth = g
            break
    if pe is None or growth is None:
        return {**base, "action": "数据不足", "tone": "neutral", "buy_zone": None,
                "take_profit": None, "stop_loss": None,
                "reason": "缺少 PE 或净利润增速，PEG 无法计算（成长股数据常有缺失）。"}
    if growth <= 0:
        return {**base, "action": "成长转负，不买", "tone": "bearish",
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": f"最新净利润同比 {growth:.0f}%（负增长），林奇式成长逻辑不成立。"}
    peg = pe / growth
    ma20, close = lv["ma20"], lv["close"]
    buy_zone = _zone(ma20 * 0.97, ma20 * 1.03) if ma20 else None
    stop = _round(close * 0.9) if close else None
    if peg < 1:
        return {**base, "action": "成长与估值匹配，可买", "tone": "bullish",
                "buy_zone": buy_zone, "take_profit": None, "stop_loss": stop,
                "reason": (f"PE={pe:.0f}、净利增速 {growth:.0f}% → PEG≈{peg:.2f}(<1) 偏低，"
                           f"成长性价比好；回踩 MA20 分批，PEG>1.5 或成长转负再减，止损约 −10%。")}
    if peg <= 1.5:
        return {**base, "action": "估值合理，持有为主", "tone": "neutral",
                "buy_zone": buy_zone, "take_profit": None, "stop_loss": stop,
                "reason": f"PEG≈{peg:.2f} 处于合理区，性价比一般；持有者拿住，新仓等回调。"}
    return {**base, "action": "成长跟不上估值，偏贵", "tone": "bearish",
            "buy_zone": None, "take_profit": None, "stop_loss": None,
            "reason": f"PE={pe:.0f}、增速 {growth:.0f}% → PEG≈{peg:.2f}(>1.5) 偏贵，林奇会等回调。"}


_BUY_ACTION = ("买", "建仓", "反弹")
_REDUCE_ACTION = ("减", "回避", "不买", "偏贵", "卖")


def _stance(masters: list[dict]) -> dict:
    """综合各大师，给一个总体操作倾向。"""
    buy = sum(1 for m in masters if any(w in m["action"] for w in _BUY_ACTION) and m["tone"] != "bearish")
    reduce = sum(1 for m in masters if m["tone"] == "bearish" or any(w in m["action"] for w in _REDUCE_ACTION))
    triggered = [m for m in masters if m["action"] not in ("数据不足", "未触发", "观望（趋势未确认）")]
    if buy >= 2 and reduce == 0:
        label, tone = "多位大师偏买入 / 分批", "bullish"
    elif reduce >= 2 and buy == 0:
        label, tone = "多位大师偏回避 / 减仓", "bearish"
    elif buy and reduce:
        label, tone = "分歧较大：买卖逻辑并存，降低仓位", "warning"
    elif buy:
        label, tone = "局部买点：仅部分逻辑成立，轻仓试", "bullish"
    elif not triggered:
        label, tone = "暂无明确买卖点，观望", "neutral"
    else:
        label, tone = "以观望 / 持有为主", "neutral"
    return {"label": label, "tone": tone, "buy": buy, "reduce": reduce}


def build_plan(df: pd.DataFrame, trends: dict | None = None,
               funda: dict | None = None) -> dict:
    """汇总：关键价位 + 四位大师的买卖纪律 + 总体倾向。"""
    if df is None or df.empty:
        return {"levels": {}, "masters": [], "stance": {}, "disclaimer": ""}
    lv = _key_levels(df)
    masters = [
        _trend_master(df, trends or {}, lv),
        _value_master(df, funda, lv),
        _growth_master(df, funda, lv),
        _rebound_master(df, lv),
    ]
    return {
        "levels": lv,
        "masters": masters,
        "stance": _stance(masters),
        "disclaimer": ("规则化研究信号，非投资建议；数据为日线非实时，价位为参考区间。"
                       "便宜可能更便宜、趋势可能反转——务必带止损、控制仓位，先用模拟盘验证。"),
    }


def plan_text(plan: dict, pref: str = "balanced") -> str:
    """把操作计划压缩成给 LLM 的中文上下文（含价位）。"""
    if not plan or not plan.get("masters"):
        return ""
    lv = plan.get("levels") or {}
    lines = [f"关键价位：现价 {lv.get('close')}，近端支撑 {lv.get('support')}，"
             f"近端压力 {lv.get('resistance')}，MA20 {lv.get('ma20')}，MA60 {lv.get('ma60')}，"
             f"布林下轨 {lv.get('boll_lower')}，布林上轨 {lv.get('boll_upper')}。"]
    for m in plan["masters"]:
        bz = f"买入区{m['buy_zone']}" if m.get("buy_zone") else "无明确买区"
        tp = f"，止盈≈{m['take_profit']}" if m.get("take_profit") is not None else ""
        sl = f"，止损≈{m['stop_loss']}" if m.get("stop_loss") is not None else ""
        lines.append(f"- 【{m['name']}/{m['horizon']}】{m['action']}：{bz}{tp}{sl}。依据：{m['reason']}")
    st = plan.get("stance") or {}
    lines.append(f"总体倾向：{st.get('label', '')}")
    return "大师买卖纪律（规则引擎，含具体价位）：\n" + "\n".join(lines)
