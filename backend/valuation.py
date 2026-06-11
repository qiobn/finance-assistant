"""估值锚：用多种方法估「合理价区间」并给安全边际（margin of safety）。

不依赖额外的现金流抓取（A 股免费源不稳、口径杂），改用现有数据自洽推导：
- EPS_ttm = 现价 / PE(TTM)，BVPS = 现价 / PB（同一时点，口径一致、稳健）
- 方法：历史 PE 中枢、历史 PB 中枢、格雷厄姆数、林奇合理 PE、两阶段盈利贴现(简化 DCF)
- 多法取「中位数」为合理价中枢，min/max 为区间；安全边际 = (中枢 − 现价) / 中枢

边界：这是相对/简化估值的「锚」，不是精确内在价值。对亏损股、强周期股、
银行/地产、商业模式剧变的公司会失真；务必与价格、资金、基本面互相印证，非投资建议。
"""
from __future__ import annotations

import math


def _f(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f  # 过滤 nan
    except (TypeError, ValueError):
        return None


def estimate(price, funda: dict | None, growth=None) -> dict:
    """price: 现价；funda: 基本面(含 valuation)；growth: 净利润同比增速(%)。"""
    price = _f(price)
    v = (funda or {}).get("valuation") or {}
    pe, pe_med = _f(v.get("pe_ttm")), _f(v.get("pe_ttm_median"))
    pb, pb_med = _f(v.get("pb")), _f(v.get("pb_median"))
    growth = _f(growth)

    if price is None or price <= 0:
        return {"methods": [], "note": "缺少现价，无法估值。"}

    eps = price / pe if (pe and pe > 0) else None      # TTM 每股收益
    bvps = price / pb if (pb and pb > 0) else None      # 每股净资产
    methods: list[dict] = []

    if pe and pe > 0 and pe_med and pe_med > 0:
        methods.append({"name": "历史PE中枢", "fair": round(price * pe_med / pe, 2),
                        "note": f"PE {pe:.1f}→近五年中位 {pe_med:.1f}"})
    if pb and pb > 0 and pb_med and pb_med > 0:
        methods.append({"name": "历史PB中枢", "fair": round(price * pb_med / pb, 2),
                        "note": f"PB {pb:.2f}→近五年中位 {pb_med:.2f}"})
    if eps and eps > 0 and bvps and bvps > 0:
        methods.append({"name": "格雷厄姆数", "fair": round(math.sqrt(22.5 * eps * bvps), 2),
                        "note": f"√(22.5×EPS{eps:.2f}×BVPS{bvps:.2f})"})
    if eps and eps > 0 and growth is not None and growth > 0:
        fair_pe = min(max(growth, 8), 40)  # 合理 PE≈增速，封顶 40 防极端
        methods.append({"name": "林奇合理PE", "fair": round(eps * fair_pe, 2),
                        "note": f"EPS{eps:.2f}×合理PE{fair_pe:.0f}(≈增速)"})
    if eps and eps > 0:
        g1 = min(max((growth if growth is not None else 8) / 100.0, 0.0), 0.20)
        r, g2, n = 0.09, 0.03, 10  # 贴现率 9%、永续 3%、显式期 10 年
        pv, e = 0.0, eps
        for t in range(1, n + 1):
            e *= (1 + g1)
            pv += e / ((1 + r) ** t)
        term = (e * (1 + g2) / (r - g2)) / ((1 + r) ** n)
        methods.append({"name": "盈利贴现(简化DCF)", "fair": round(pv + term, 2),
                        "note": f"EPS{eps:.2f}、前5年增速{g1 * 100:.0f}%、贴现9%/永续3%"})

    fairs = sorted(m["fair"] for m in methods if m.get("fair") and m["fair"] > 0)
    if len(fairs) < 2:
        return {"price": round(price, 2), "methods": methods,
                "note": "可用估值方法不足（可能亏损/估值数据缺失），仅供参考。"}

    k = len(fairs)
    mid = fairs[k // 2] if k % 2 else round((fairs[k // 2 - 1] + fairs[k // 2]) / 2, 2)
    low, high = fairs[0], fairs[-1]
    mos = (mid - price) / mid  # 安全边际：正=低估、负=高估
    if mos >= 0.20:
        verdict, tone = "低估", "bullish"
    elif mos <= -0.15:
        verdict, tone = "高估", "bearish"
    else:
        verdict, tone = "合理", "neutral"
    spread = (high - low) / mid if mid else 0
    note = "多法一致性较好" if spread <= 0.5 else "各法分歧较大，区间仅供参考"
    return {"price": round(price, 2), "methods": methods,
            "fair_low": round(low, 2), "fair_mid": round(mid, 2), "fair_high": round(high, 2),
            "mos": round(mos * 100, 1), "verdict": verdict, "tone": tone, "note": note}


def short_text(val: dict | None) -> str:
    """一句话摘要，供大师 reason / LLM 上下文复用。"""
    if not val or not val.get("fair_mid"):
        return ""
    return (f"多法估值合理价≈{val['fair_mid']}（区间 {val['fair_low']}~{val['fair_high']}），"
            f"现价 {val['price']}，安全边际 {val['mos']:+.0f}%（{val['verdict']}；{val['note']}）。")
