"""智能体工具层：把项目已有能力包装成可被 LLM 调用的 function-calling 工具。

设计：
- 每个工具返回**紧凑、可 JSON 序列化**的 dict（裁剪字段，控制 token）。
- 只依赖底层模块（data/intel/strategies/backtest/storage/analysis），不 import app，避免循环依赖。
"""
from __future__ import annotations

from . import backtest, data, intel, storage, strategies
from .analysis import build_analysis, trend_overview
from .indicators import compute_all


def _zfill(code: str) -> str:
    return str(code or "").strip().zfill(6)


# ---------------- 各工具实现 ----------------
def t_search_stocks(query: str = "", **_) -> dict:
    res = data.search_stocks(query, limit=10)
    return {"results": [{"code": r["code"], "name": r["name"]} for r in res]}


def _analysis(code: str):
    df, is_demo = data.get_kline(code, days=160, adjust="qfq")
    if df is None or df.empty:
        return None, None, None
    df = compute_all(df)
    return df, build_analysis(df), is_demo


def t_get_stock_analysis(code: str = "", **_) -> dict:
    code = _zfill(code)
    df, a, is_demo = _analysis(code)
    if df is None:
        return {"error": f"未取到 {code} 的行情数据"}
    last = df.iloc[-1]
    prev = float(df.iloc[-2]["close"]) if len(df) > 1 else float(last["close"])
    pct = round((float(last["close"]) / prev - 1) * 100, 2) if prev else 0.0
    tr = trend_overview(df)
    return {
        "code": code, "name": data.name_for(code), "is_demo": is_demo,
        "close": round(float(last["close"]), 2), "pct": pct,
        "verdict": a["verdict"], "counts": a["counts"],
        "signals": [{"dim": s["dim"], "label": s["label"], "tone": s["tone"]} for s in a["signals"]],
        "trend": {k: v["label"] for k, v in (tr.get("items") or {}).items()},
        "trend_align": (tr.get("align") or {}).get("label"),
    }


def t_get_fundamentals(code: str = "", **_) -> dict:
    code = _zfill(code)
    f = data.get_fundamentals(code)
    v = f.get("valuation") or {}
    out = {"code": code, "name": data.name_for(code), "valuation": {}, "finance_latest": {}, "fund_flow": {}}
    for k in ("pe_ttm", "pe_ttm_pct", "pe_ttm_median", "pb", "pb_pct", "pb_median"):
        if v.get(k) is not None:
            out["valuation"][k] = v[k]
    fins = f.get("financials") or []
    if fins:  # 最近一期财务（financials 已按从新到旧排列）
        out["finance_latest"] = {k: val for k, val in fins[0].items() if val is not None}
    ff = f.get("fund_flow") or {}
    if ff and ff.get("main_net") is not None:
        out["fund_flow"] = {"main_net_wan": ff.get("main_net"), "date": ff.get("date")}
    return out


def t_get_master_plan(code: str = "", **_) -> dict:
    code = _zfill(code)
    df, is_demo = data.get_kline(code, days=160, adjust="qfq")
    if df is None or df.empty:
        return {"error": f"未取到 {code} 的行情数据"}
    df = compute_all(df)
    funda = data.get_fundamentals(code)
    plan = strategies.build_plan(df, trend_overview(df), funda)
    val = plan.get("valuation") or {}
    return {
        "code": code, "name": data.name_for(code),
        "levels": plan.get("levels", {}),
        "stance": plan.get("stance", {}),
        "valuation": {k: val.get(k) for k in ("fair_low", "fair_mid", "fair_high", "mos", "verdict")
                      if val.get(k) is not None},
        "masters": [
            {"name": m.get("name"), "horizon": m.get("horizon"), "action": m.get("action"),
             "score": m.get("score"), "confidence": m.get("confidence"),
             "buy_zone": m.get("buy_zone"), "take_profit": m.get("take_profit"),
             "stop_loss": m.get("stop_loss"), "reason": m.get("reason")}
            for m in (plan.get("masters") or [])
        ],
    }


def t_run_backtest(code: str = "", strategy: str = "composite", days: int = 250, **_) -> dict:
    code = _zfill(code)
    r = backtest.run(code, strategy=strategy, days=int(days or 250), adjust="qfq")
    if r.get("error"):
        return {"error": r["error"]}
    return {"code": code, "name": r.get("name"), "strategy": r.get("strategy_label"),
            "period": f"{r.get('start')} ~ {r.get('end')}", "metrics": r.get("metrics", {})}


def t_get_market_overview(**_) -> dict:
    mk = data.get_index_overview()
    return {"indices": [{"name": i["name"], "price": i["price"], "pct": i["pct"]}
                        for i in (mk.get("indices") or [])],
            "breadth": mk.get("breadth", {}), "is_demo": mk.get("is_demo", False)}


def t_get_sector_board(kind: str = "industry", **_) -> dict:
    s = intel.get_sector_board("concept" if kind == "concept" else "industry")
    return {"leaders": [{"name": x["name"], "pct": x["pct"]} for x in (s.get("leaders") or [])[:8]],
            "laggards": [{"name": x["name"], "pct": x["pct"]} for x in (s.get("laggards") or [])[:5]]}


def t_get_global_news(limit: int = 12, **_) -> dict:
    n = intel.get_global_news(min(int(limit or 12), 30))
    items = []
    for x in (n.get("items") or [])[:int(limit or 12)]:
        items.append({"title": x.get("title"), "time": x.get("time"),
                      "sentiment": x.get("sentiment"), "sectors": (x.get("sectors") or [])[:3]})
    return {"items": items}


def t_get_stock_news(code: str = "", limit: int = 8, **_) -> dict:
    code = _zfill(code)
    n = intel.get_stock_news(code, min(int(limit or 8), 20))
    return {"code": code, "items": [{"title": x.get("title"), "time": x.get("time"),
                                     "source": x.get("source")} for x in (n.get("items") or [])[:int(limit or 8)]]}


def t_get_watchlist(**_) -> dict:
    codes = storage.load()
    return {"items": [{"code": c, "name": data.name_for(c)} for c in codes]}


def t_get_portfolio(**_) -> dict:
    positions = storage.load_positions()
    items = []
    for pos in positions:
        code = _zfill(pos.get("code"))
        shares = float(pos.get("shares") or 0)
        cost = float(pos.get("cost") or 0)
        df, a, is_demo = _analysis(code)
        if df is None or is_demo:
            items.append({"code": code, "name": data.name_for(code), "error": "无实时行情"})
            continue
        rt = data.latest_price(code)
        price = rt if rt else float(df.iloc[-1]["close"])
        pnl_pct = round((price / cost - 1) * 100, 2) if cost > 0 else 0.0
        items.append({"code": code, "name": data.name_for(code), "shares": shares, "cost": cost,
                      "price": round(price, 3), "pnl_pct": pnl_pct, "verdict": a["verdict"]})
    return {"items": items}


# ---------------- 工具规格（OpenAI function-calling 格式）----------------
def _spec(name: str, desc: str, props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


_CODE = {"code": {"type": "string", "description": "6 位股票或ETF代码，如 600519、510300"}}

TOOLS = [
    _spec("search_stocks", "按名称或代码模糊搜索股票/ETF，拿到准确的 6 位代码。",
          {"query": {"type": "string", "description": "股票或ETF的名称/代码关键词"}}, ["query"]),
    _spec("get_stock_analysis", "获取个股技术面：综合判断、信号灯(趋势/MACD/RSI/布林/量能)、多周期趋势、现价涨跌。", _CODE, ["code"]),
    _spec("get_fundamentals", "获取个股基本面：PE/PB及历史分位、股息率、市值、关键财务、主力资金流。", _CODE, ["code"]),
    _spec("get_master_plan", "获取『投资大师』操作计划：买入区/止盈/止损价位、各大师多空分与置信度、委员会总体倾向、估值锚与安全边际。", _CODE, ["code"]),
    _spec("run_backtest", "对个股运行某技术策略历史回测，返回收益/超额/胜率/最大回撤/夏普等。",
          {**_CODE,
           "strategy": {"type": "string", "enum": ["composite", "trend", "macd", "rebound"],
                        "description": "策略：composite综合/trend趋势/macd金叉/rebound超跌反弹"},
           "days": {"type": "integer", "description": "回测交易日数，默认250"}}, ["code"]),
    _spec("get_market_overview", "获取大盘行情：主要指数点位涨跌 + 全市场涨跌家数。"),
    _spec("get_sector_board", "获取行业/概念板块热度榜（领涨/领跌）。",
          {"kind": {"type": "string", "enum": ["industry", "concept"], "description": "industry行业/concept概念"}}),
    _spec("get_global_news", "获取最新财经快讯（含已缓存的情绪标签）。",
          {"limit": {"type": "integer", "description": "条数，默认12，最多30"}}),
    _spec("get_stock_news", "获取某只个股的近期相关新闻。",
          {**_CODE, "limit": {"type": "integer", "description": "条数，默认8"}}, ["code"]),
    _spec("get_watchlist", "获取用户的自选股列表（代码与名称）。"),
    _spec("get_portfolio", "获取用户的实际持仓：代码/数量/成本/现价/浮动盈亏%/技术判断。"),
]

TOOL_FUNCS = {
    "search_stocks": t_search_stocks,
    "get_stock_analysis": t_get_stock_analysis,
    "get_fundamentals": t_get_fundamentals,
    "get_master_plan": t_get_master_plan,
    "run_backtest": t_run_backtest,
    "get_market_overview": t_get_market_overview,
    "get_sector_board": t_get_sector_board,
    "get_global_news": t_get_global_news,
    "get_stock_news": t_get_stock_news,
    "get_watchlist": t_get_watchlist,
    "get_portfolio": t_get_portfolio,
}

TOOL_LABELS = {
    "search_stocks": "搜索股票", "get_stock_analysis": "技术面分析", "get_fundamentals": "基本面",
    "get_master_plan": "大师操作计划", "run_backtest": "策略回测", "get_market_overview": "大盘行情",
    "get_sector_board": "板块热度", "get_global_news": "财经快讯", "get_stock_news": "个股新闻",
    "get_watchlist": "自选股", "get_portfolio": "持仓",
}


def execute(name: str, args: dict) -> dict:
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return {"error": f"未知工具 {name}"}
    try:
        return fn(**(args or {}))
    except Exception as exc:  # 工具失败不应中断整个对话
        return {"error": f"{name} 执行失败：{str(exc)[:120]}"}
