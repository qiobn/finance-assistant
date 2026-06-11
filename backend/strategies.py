"""投资大师 Skills：把行情/指标/估值翻译成「可执行的买卖纪律」。

每位大师 = 一套规则，输出：当前该做什么(action) + 买入区间(buy_zone)
+ 止盈位/条件(take_profit) + 止损位(stop_loss) + 依据(reason)。

重要边界：这些是**规则化研究信号**，不是投资建议，更不是「保证买卖点」。
数据为日线级别、非实时；价位为参考区间，实际下单以你的行情软件为准。
大师不预测「几月几号买」，而是给「在什么位置、满足什么条件就买/卖」。
"""
from __future__ import annotations

import statistics

import pandas as pd

from . import valuation


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


def _clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


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
    # 置信度：周/月同向 → 高；只有其一 → 中；都缺 → 低
    agree = wk is not None and wk == mo
    conf = 78 if agree else (58 if (wk or mo) else 38)
    if down_big or (wk == "bearish" and mo != "bullish"):
        return {**base, "action": "回避 / 不逆势抄底", "tone": "bearish",
                "score": 20, "confidence": conf, "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": "周/月线大方向向下，顺势纪律是不买、空仓等右侧信号；逆势抄底胜率低。"}
    if not up_big:
        return {**base, "action": "观望（趋势未确认）", "tone": "neutral",
                "score": 48, "confidence": 38, "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": "周/月线方向不明，等大级别转多或日线放量突破再跟。"}

    buy_zone = _zone(ma20 * 0.97, ma20 * 1.03) if ma20 else None
    take_profit = lv["boll_upper"]
    stop = _round(min(x for x in (ma60, close * 0.92 if close else None) if x is not None)) \
        if (ma60 or close) else None
    if rsi is not None and rsi >= 75:
        action, tone, score = "减仓 / 不追高", "warning", 46
        reason = (f"大方向向上但 RSI={rsi:.0f} 超买、贴近上轨，"
                  f"顺势者在此减仓锁利，等回踩 MA20({ma20}) 再接。")
    elif close and ma20 and abs(close / ma20 - 1) <= 0.03:
        action, tone, score = "逢回踩分批买入", "bullish", 80
        reason = (f"周/月线向上，价回踩到 MA20({ma20}) 附近企稳，是顺势买点；"
                  f"买入区≈{buy_zone}，跌破 MA60({ma60}) 或 −8% 离场。")
    elif close and ma20 and close > ma20 * 1.03:
        action, tone, score = "持有 / 等回踩", "neutral", 60
        reason = (f"趋势向上但已偏离 MA20({ma20})，追高性价比低；"
                  f"持有者拿住，新仓等回踩到 {buy_zone} 再进。")
    else:
        action, tone, score = "趋势偏多，控制仓位", "bullish", 70
        reason = f"大方向向上，{('MACD金叉、' if macd == 'golden' else '')}回踩 MA20({ma20}) 是相对安全的买点。"
    if macd == "golden":
        score = _clamp(score + 4)
    elif macd == "below":
        score = _clamp(score - 4)
    return {**base, "action": action, "tone": tone, "score": round(score), "confidence": conf,
            "buy_zone": buy_zone, "take_profit": take_profit, "stop_loss": stop, "reason": reason}


def _value_master(df: pd.DataFrame, funda: dict | None, lv: dict, val: dict | None = None) -> dict:
    """价值/逆向（格雷厄姆·巴菲特·Burry）：近五年估值分位 + 多法合理价/安全边际。"""
    base = {"key": "value", "name": "价值 / 逆向（格雷厄姆·巴菲特）", "horizon": "中长线"}
    v = (funda or {}).get("valuation") or {}
    pe_pct = _f(v.get("pe_ttm_pct"))
    pb_pct = _f(v.get("pb_pct"))
    pcts = [p for p in (pe_pct, pb_pct) if p is not None]
    close, recent_low = lv["close"], lv["recent_low"]
    vtext = valuation.short_text(val)
    mos = _f((val or {}).get("mos"))          # 安全边际%（正=低估）
    verdict = (val or {}).get("verdict")
    n_methods = len((val or {}).get("methods") or [])

    # 估值分位缺失时，用多法合理价的安全边际兜底判断
    if not pcts:
        if mos is not None:
            score = round(_clamp(50 + mos * 1.4))      # +20%→78、-15%→29
            conf = round(_clamp(45 + n_methods * 7))    # 方法越多越可信
            if verdict == "低估":
                return {**base, "action": "多法估值低估，可分批", "tone": "bullish",
                        "score": score, "confidence": conf,
                        "buy_zone": _zone(recent_low, close), "take_profit": None,
                        "stop_loss": _round(close * 0.85) if close else None,
                        "reason": f"无分位数据，但{vtext}有安全边际，价值派可分批买、基本面恶化才离场。"}
            if verdict == "高估":
                return {**base, "action": "多法估值偏高，不买", "tone": "bearish",
                        "score": score, "confidence": conf,
                        "buy_zone": None, "take_profit": None, "stop_loss": None,
                        "reason": f"无分位数据；{vtext}缺乏安全边际，价值派此时不追。"}
            return {**base, "action": "估值中性，耐心等", "tone": "neutral",
                    "score": score, "confidence": round(_clamp(conf - 10)),
                    "buy_zone": None, "take_profit": None, "stop_loss": None,
                    "reason": f"{vtext}估值不便宜也不贵，价值派等更明显的低估。"}
        return {**base, "action": "数据不足", "tone": "neutral", "score": 50, "confidence": 12,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": "暂无近五年 PE/PB 分位与估值数据，价值视角无法判断（亏损股估值也会失真）。"}

    low = min(pcts)
    # 评分：分位越低越便宜分越高，与多法安全边际融合
    score_pct = 100 - low
    score = round(_clamp(0.6 * score_pct + 0.4 * _clamp(50 + mos * 1.4)) if mos is not None else score_pct)
    # 置信度：分位双指标齐全、估值方法多、信号越极端越可信
    conf = round(_clamp(58 + (10 if len(pcts) == 2 else 0) + min(n_methods, 4) * 4 + abs(score - 50) * 0.2))
    tag = " / ".join(filter(None, [
        f"PE分位{pe_pct:.0f}%" if pe_pct is not None else None,
        f"PB分位{pb_pct:.0f}%" if pb_pct is not None else None,
    ]))
    suffix = (" " + vtext) if vtext else ""
    if low <= 20:
        return {**base, "action": "估值很便宜，积极分批", "tone": "bullish",
                "score": score, "confidence": conf,
                "buy_zone": _zone(recent_low, close), "take_profit": None,
                "stop_loss": _round(close * 0.85) if close else None,
                "reason": (f"{tag}，处于近五年低位区（安全边际较好）。价值派分批买、越跌越买；"
                           f"估值修复到贵区(>70%分位)再考虑减；止损放宽到约 −15%。" + suffix)}
    if low <= 30:
        return {**base, "action": "偏便宜，可小批建仓", "tone": "bullish",
                "score": score, "confidence": conf,
                "buy_zone": _zone(recent_low, close), "take_profit": None,
                "stop_loss": _round(close * 0.85) if close else None,
                "reason": f"{tag}，估值偏低。可小批建仓，留子弹等更低；分位回到 70%+ 时分批减。" + suffix}
    if low >= 70:
        word = "（多法亦显示高估）" if verdict == "高估" else ""
        return {**base, "action": "估值偏贵，不买 / 可减", "tone": "bearish",
                "score": score, "confidence": conf,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": f"{tag}，处于近五年高位区{word}，缺乏安全边际；价值派不追，持仓者考虑分批兑现。" + suffix}
    if verdict == "低估":
        return {**base, "action": "分位中性但多法低估，可小批", "tone": "bullish",
                "score": score, "confidence": round(_clamp(conf - 8)),
                "buy_zone": _zone(recent_low, close), "take_profit": None,
                "stop_loss": _round(close * 0.85) if close else None,
                "reason": f"{tag}（分位中性），但{vtext}有安全边际，价值派可小批参与。"}
    return {**base, "action": "估值中性，耐心等", "tone": "neutral",
            "score": score, "confidence": round(_clamp(conf - 8)),
            "buy_zone": None, "take_profit": None, "stop_loss": None,
            "reason": f"{tag}，估值不算便宜也不算贵；价值派偏好等更明显的低估再出手。" + suffix}


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
        # RSI 越低、越贴下轨，反弹博弈分越高；但本就是短线高风险，置信度封顶中等
        score = round(_clamp(62 + (30 - rsi)))
        conf = round(_clamp(50 + (30 - rsi) * 1.2))
        return {**base, "action": "博反弹（小仓位）", "tone": "bullish",
                "score": score, "confidence": conf,
                "buy_zone": buy_zone, "take_profit": tp, "stop_loss": stop,
                "reason": (f"RSI={rsi:.0f} 超卖且贴近布林下轨({bl})，有超跌反弹机会；"
                           f"目标看中轨/MA20({tp})，跌破近期低点约 −4% 必须止损。高风险、仓位要小。")}
    why = []
    if rsi is not None:
        why.append(f"RSI={rsi:.0f}{'（未超卖）' if rsi > 30 else ''}")
    if not near_lower:
        why.append("未贴近布林下轨")
    return {**base, "action": "未触发", "tone": "neutral", "score": 50, "confidence": 30,
            "buy_zone": None, "take_profit": None, "stop_loss": None,
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
        return {**base, "action": "数据不足", "tone": "neutral", "score": 50, "confidence": 12,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": "缺少 PE 或净利润增速，PEG 无法计算（成长股数据常有缺失）。"}
    if growth <= 0:
        return {**base, "action": "成长转负，不买", "tone": "bearish", "score": 24, "confidence": 70,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": f"最新净利润同比 {growth:.0f}%（负增长），林奇式成长逻辑不成立。"}
    peg = pe / growth
    # PEG 越低分越高：0.5→~85、1.0→~62、1.5→~45、2+→低
    score = round(_clamp(110 - peg * 48))
    conf = round(_clamp(60 + abs(score - 50) * 0.3))
    ma20, close = lv["ma20"], lv["close"]
    buy_zone = _zone(ma20 * 0.97, ma20 * 1.03) if ma20 else None
    stop = _round(close * 0.9) if close else None
    if peg < 1:
        return {**base, "action": "成长与估值匹配，可买", "tone": "bullish",
                "score": score, "confidence": conf,
                "buy_zone": buy_zone, "take_profit": None, "stop_loss": stop,
                "reason": (f"PE={pe:.0f}、净利增速 {growth:.0f}% → PEG≈{peg:.2f}(<1) 偏低，"
                           f"成长性价比好；回踩 MA20 分批，PEG>1.5 或成长转负再减，止损约 −10%。")}
    if peg <= 1.5:
        return {**base, "action": "估值合理，持有为主", "tone": "neutral",
                "score": score, "confidence": conf,
                "buy_zone": buy_zone, "take_profit": None, "stop_loss": stop,
                "reason": f"PEG≈{peg:.2f} 处于合理区，性价比一般；持有者拿住，新仓等回调。"}
    return {**base, "action": "成长跟不上估值，偏贵", "tone": "bearish",
            "score": score, "confidence": conf,
            "buy_zone": None, "take_profit": None, "stop_loss": None,
            "reason": f"PE={pe:.0f}、增速 {growth:.0f}% → PEG≈{peg:.2f}(>1.5) 偏贵，林奇会等回调。"}


def _latest_fin(funda: dict | None, col: str) -> float | None:
    """财务摘要里某指标的最近一期非空值。"""
    for row in ((funda or {}).get("financials") or []):
        val = _f(row.get(col))
        if val is not None:
            return val
    return None


def _annual_fin(funda: dict | None, col: str) -> float | None:
    """优先取最近『年报(12-31)』的指标——ROE 等累计型指标按季报会被严重低估。

    找不到年报时退回最近一期非空值。
    """
    rows = (funda or {}).get("financials") or []
    for row in rows:
        period = str(row.get("报告期", ""))
        if period.endswith("12-31"):
            val = _f(row.get(col))
            if val is not None:
                return val
    return _latest_fin(funda, col)


def _quality_master(df: pd.DataFrame, funda: dict | None, lv: dict, val: dict | None = None) -> dict:
    """芒格 · 质量/护城河：好生意（高 ROE、高毛利、低负债）+ 价格是否合理。"""
    base = {"key": "quality", "name": "质量护城河（芒格）", "horizon": "中长线"}
    roe = _annual_fin(funda, "净资产收益率")  # ROE 用年报，避免季报累计值低估
    margin = _latest_fin(funda, "销售毛利率")
    debt = _latest_fin(funda, "资产负债率")
    if roe is None and margin is None:
        return {**base, "action": "数据不足", "tone": "neutral", "score": 50, "confidence": 12,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": "缺少 ROE/毛利率等质量指标，无法判断是不是『好生意』。"}
    v = (funda or {}).get("valuation") or {}
    pcts = [p for p in (_f(v.get("pe_ttm_pct")), _f(v.get("pb_pct"))) if p is not None]
    val_pct = min(pcts) if pcts else None
    # 质量分：ROE/毛利/负债各项打分，再按估值高低小幅调整
    q = 50.0
    if roe is not None:
        q += _clamp((roe - 12) * 2.2, -24, 26)
    if margin is not None:
        q += _clamp((margin - 25) * 0.5, -12, 14)
    if debt is not None:
        q += _clamp((55 - debt) * 0.25, -12, 10)
    if val_pct is not None:
        q += _clamp((50 - val_pct) * 0.2, -10, 10)   # 越便宜略加分
    q_score = round(_clamp(q))
    n_q = sum(x is not None for x in (roe, margin, debt))
    q_conf = round(_clamp(50 + n_q * 8 + (8 if val_pct is not None else 0)))
    bits = " / ".join(filter(None, [
        f"ROE {roe:.1f}%" if roe is not None else None,
        f"毛利 {margin:.0f}%" if margin is not None else None,
        f"负债率 {debt:.0f}%" if debt is not None else None,
    ]))
    good = ((roe is None or roe >= 12) and (margin is None or margin >= 25)
            and (debt is None or debt <= 65) and (roe is not None or margin is not None))
    strong = (roe is not None and roe >= 18) and (margin is None or margin >= 30)
    close, ma20, ma60 = lv["close"], lv["ma20"], lv["ma60"]
    buy_zone = _zone(ma20 * 0.97, ma20 * 1.03) if ma20 else None
    stop = _round(min(x for x in (ma60, close * 0.9 if close else None) if x is not None)) if (ma60 or close) else None
    if not good:
        weak = []
        if roe is not None and roe < 12:
            weak.append("ROE 偏低")
        if margin is not None and margin < 25:
            weak.append("毛利偏薄")
        if debt is not None and debt > 65:
            weak.append("负债偏高")
        return {**base, "action": "生意质量一般，不碰", "tone": "bearish",
                "score": min(q_score, 42), "confidence": q_conf,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": f"{bits}：{('、'.join(weak)) or '质量不达标'}。芒格只买伟大的生意，宁可错过不将就。"}
    vtext = valuation.short_text(val)
    verdict = (val or {}).get("verdict")
    rich = (val_pct is not None and val_pct >= 70) or verdict == "高估"
    if rich:
        return {**base, "action": "好生意但偏贵，等回调", "tone": "neutral",
                "score": min(q_score, 58), "confidence": q_conf,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": (f"{bits}，确是好生意；但当前估值偏贵（"
                           + (f"近五年 {val_pct:.0f}% 分位" if val_pct is not None else "")
                           + "），合理价才出手，耐心等回调。" + (" " + vtext if vtext else ""))}
    quality_word = "卓越" if strong else "稳健"
    return {**base, "action": "好生意+价格合理，可买/持有", "tone": "bullish",
            "score": max(q_score, 62), "confidence": q_conf,
            "buy_zone": buy_zone, "take_profit": None, "stop_loss": stop,
            "reason": (f"{bits}，{quality_word}的生意"
                       + (f"，估值近五年 {val_pct:.0f}% 分位不贵" if val_pct is not None else "")
                       + f"。芒格式『好生意合理价』，回踩 MA20({ma20}) 分批、长期持有，基本面恶化才离场。"
                       + (" " + vtext if vtext else ""))}


def _wood_master(df: pd.DataFrame, funda: dict | None, lv: dict) -> dict:
    """凯茜·伍德 · 高成长/颠覆：营收高增长可容忍高估值，成长熄火则离场。"""
    base = {"key": "aggressive_growth", "name": "高成长颠覆（凯茜·伍德）", "horizon": "中长线"}
    rev_g = _latest_fin(funda, "营业总收入同比增长率")
    np_g = _latest_fin(funda, "净利润同比增长率")
    if rev_g is None and np_g is None:
        return {**base, "action": "数据不足", "tone": "neutral", "score": 50, "confidence": 12,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": "缺少营收/净利增速，无法判断成长爆发力。"}
    g = rev_g if rev_g is not None else np_g
    # 增速越高分越高：0%→~40、25%→~72、50%→~90
    score = round(_clamp(40 + g * 1.0))
    conf = round(_clamp(52 + (10 if (rev_g is not None and np_g is not None) else 0) + abs(score - 50) * 0.2))
    bits = " / ".join(filter(None, [
        f"营收增速 {rev_g:.0f}%" if rev_g is not None else None,
        f"净利增速 {np_g:.0f}%" if np_g is not None else None,
    ]))
    close, ma20 = lv["close"], lv["ma20"]
    buy_zone = _zone(ma20 * 0.95, close) if (ma20 and close) else None
    stop = _round(close * 0.82) if close else None  # 高成长波动大，止损放宽
    if g >= 25:
        return {**base, "action": "高成长，看长做多", "tone": "bullish",
                "score": score, "confidence": conf,
                "buy_zone": buy_zone, "take_profit": None, "stop_loss": stop,
                "reason": (f"{bits}，处于高速增长期。伍德重赛道与成长斜率、容忍阶段性高估值，"
                           f"回调即分批、拿长线；但成长一旦转负要果断退出（止损放宽到约 −18%）。")}
    if g <= 0:
        return {**base, "action": "成长熄火，退出", "tone": "bearish",
                "score": score, "confidence": conf,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": f"{bits}（零或负增长），颠覆成长逻辑不成立，伍德式策略不参与。"}
    return {**base, "action": "成长平庸，吸引力不足", "tone": "neutral",
            "score": score, "confidence": round(_clamp(conf - 8)),
            "buy_zone": None, "take_profit": None, "stop_loss": None,
            "reason": f"{bits}，增速不够亮眼；伍德偏好爆发式成长，此处性价比一般。"}


def _risk_master(df: pd.DataFrame, lv: dict) -> dict:
    """塔勒布 · 风险经理：波动率/最大回撤/盈亏比 → 给建议仓位与尾部风险提示。"""
    base = {"key": "risk", "name": "风险经理（塔勒布）", "horizon": "风控"}
    close = df["close"].astype(float)
    rets = close.pct_change().dropna().tail(120)
    if len(rets) < 20:
        return {**base, "action": "数据不足", "tone": "neutral", "score": None, "confidence": 15,
                "buy_zone": None, "take_profit": None, "stop_loss": None,
                "reason": "样本不足，无法评估波动与回撤。"}
    ann_vol = float(rets.std() * (244 ** 0.5) * 100)
    win = close.tail(120)
    max_dd = float((win / win.cummax() - 1).min() * 100)

    # 盈亏比（不对称性）：上行到压力 vs 下行到支撑
    c, sup, res = lv["close"], lv["support"], lv["resistance"]
    rr = None
    if c and sup and res and c > sup and res > c:
        up = (res - c) / c
        down = (c - sup) / c
        rr = round(up / down, 2) if down > 0 else None

    if ann_vol > 60 or max_dd < -50:
        pos, tone = "≤ 10%", "warning"
    elif ann_vol > 45:
        pos, tone = "≤ 20%", "warning"
    elif ann_vol > 30:
        pos, tone = "≤ 30%", "neutral"
    else:
        pos, tone = "≤ 40%", "neutral"
    rr_text = (f"盈亏比≈{rr}（{'不对称占优，值得下注' if rr and rr >= 2 else '一般/偏差，谨慎'}）；"
               if rr is not None else "")
    return {**base, "action": f"建议单票仓位 {pos}", "tone": tone,
            "score": None, "confidence": round(_clamp(60 + min(len(rets), 120) * 0.2)),
            "buy_zone": None, "take_profit": None, "stop_loss": None,
            "reason": (f"年化波动率≈{ann_vol:.0f}%、近120日最大回撤≈{max_dd:.0f}%。{rr_text}"
                       f"塔勒布只在『亏损有限、盈利可观』时下注：务必先定止损，再按上限建议控制仓位，"
                       f"分批进出、避免一把梭被尾部风险击穿。")}


def _stance(masters: list[dict]) -> dict:
    """组合经理：对方向型大师（排除风控）按『置信度加权』汇总得分，并衡量分歧度。

    加权得分 0-100（50 中性）：Σ(score×confidence)/Σconfidence；
    分歧度 = 各大师得分的总体标准差，过大时即便均值偏多也提示降低仓位。
    """
    directional = [m for m in masters
                   if m.get("key") != "risk" and m.get("score") is not None
                   and m.get("action") != "数据不足"]
    if not directional:
        return {"label": "暂无足够依据，观望", "tone": "neutral",
                "score": 50, "confidence": 0, "dispersion": 0, "buy": 0, "reduce": 0, "n": 0}
    wsum = sum(m["confidence"] for m in directional) or 1
    wscore = sum(m["score"] * m["confidence"] for m in directional) / wsum
    avg_conf = wsum / len(directional)
    buy = sum(1 for m in directional if m["tone"] == "bullish")
    reduce = sum(1 for m in directional if m["tone"] == "bearish")
    disp = statistics.pstdev([m["score"] for m in directional]) if len(directional) > 1 else 0.0
    high_disp = disp >= 22

    if wscore >= 66 and not high_disp:
        label, tone = "委员会偏买入：多数高置信看多 / 分批", "bullish"
    elif wscore >= 58:
        label, tone = "偏买入：加权倾向看多，控制仓位", "bullish"
    elif wscore <= 34 and not high_disp:
        label, tone = "委员会偏回避：多数看空 / 空仓等右侧", "bearish"
    elif wscore <= 42:
        label, tone = "偏谨慎：加权倾向看空，观望 / 减仓", "bearish"
    elif high_disp:
        label, tone = "分歧较大：买卖逻辑并存，降低仓位", "warning"
    else:
        label, tone = "中性：以观望 / 持有为主", "neutral"
    return {"label": label, "tone": tone, "score": round(wscore), "confidence": round(avg_conf),
            "dispersion": round(disp), "buy": buy, "reduce": reduce, "n": len(directional)}


def build_plan(df: pd.DataFrame, trends: dict | None = None,
               funda: dict | None = None) -> dict:
    """汇总：关键价位 + 四位大师的买卖纪律 + 总体倾向。"""
    if df is None or df.empty:
        return {"levels": {}, "masters": [], "stance": {}, "valuation": {}, "disclaimer": ""}
    lv = _key_levels(df)
    growth = _latest_fin(funda, "净利润同比增长率")
    val = valuation.estimate(lv.get("close"), funda, growth)
    masters = [
        _value_master(df, funda, lv, val),
        _quality_master(df, funda, lv, val),
        _growth_master(df, funda, lv),
        _wood_master(df, funda, lv),
        _trend_master(df, trends or {}, lv),
        _rebound_master(df, lv),
        _risk_master(df, lv),
    ]
    return {
        "levels": lv,
        "masters": masters,
        "stance": _stance(masters),
        "valuation": val,
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
    vtext = valuation.short_text(plan.get("valuation"))
    if vtext:
        lines.append("估值锚：" + vtext)
    for m in plan["masters"]:
        bz = f"买入区{m['buy_zone']}" if m.get("buy_zone") else "无明确买区"
        tp = f"，止盈≈{m['take_profit']}" if m.get("take_profit") is not None else ""
        sl = f"，止损≈{m['stop_loss']}" if m.get("stop_loss") is not None else ""
        sc = f"[多空分{m['score']}/置信{m['confidence']}%]" if m.get("score") is not None else \
            (f"[置信{m['confidence']}%]" if m.get("confidence") is not None else "")
        lines.append(f"- 【{m['name']}/{m['horizon']}】{sc}{m['action']}：{bz}{tp}{sl}。依据：{m['reason']}")
    st = plan.get("stance") or {}
    lines.append(f"委员会（置信度加权）：{st.get('label', '')}"
                 f"｜加权多空分 {st.get('score')}/100、平均置信 {st.get('confidence')}%、"
                 f"分歧度 {st.get('dispersion')}（看多 {st.get('buy')}/看空 {st.get('reduce')}）。")
    return "大师买卖纪律（规则引擎，含具体价位、多空分0-100与置信度）：\n" + "\n".join(lines)
