"""FastAPI 应用：提供数据/分析 API，并托管前端静态页面。

启动：
    uvicorn backend.app:app --reload --port 8000
然后浏览器打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import backtest, data, db, intel, llm, storage, strategies
from .analysis import build_analysis, trend_overview
from .indicators import compute_all

app = FastAPI(title="A股 AI 辅助看板", version="0.1.0")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# 并发取数线程池（akshare 为同步调用，串行循环很慢）。worker 不宜过高，防限流。
_EXEC = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fetch")


@app.on_event("startup")
def _on_startup() -> None:
    db.init_db()
    db.maybe_cleanup(protect=set(storage.load()))


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "akshare": data.AKSHARE_AVAILABLE}


@app.get("/api/search")
def search(q: str = Query("", min_length=0)) -> dict:
    return {"results": data.search_stocks(q)}


@app.get("/api/watchlist")
def get_watchlist() -> dict:
    codes = storage.load()
    return {"items": [{"code": c, "name": data.name_for(c)} for c in codes]}


@app.post("/api/watchlist/{code}")
def add_watchlist(code: str) -> dict:
    codes = storage.add(code)
    return {"items": [{"code": c, "name": data.name_for(c)} for c in codes]}


@app.delete("/api/watchlist/{code}")
def del_watchlist(code: str) -> dict:
    codes = storage.remove(code)
    return {"items": [{"code": c, "name": data.name_for(c)} for c in codes]}


def _series(df: pd.DataFrame, col: str) -> list:
    return [None if pd.isna(v) else round(float(v), 3) for v in df[col]]


def _build_stock_payload(code: str, days: int, adjust: str) -> dict:
    df, is_demo = data.get_kline(code, days=days, adjust=adjust)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="未取到该股票数据")
    df = compute_all(df)
    analysis = build_analysis(df)
    trends = trend_overview(df)

    last = df.iloc[-1]
    prev_close = float(df.iloc[-2]["close"]) if len(df) > 1 else float(last["close"])
    change = float(last["close"]) - prev_close
    pct = (change / prev_close * 100) if prev_close else 0.0

    return {
        "code": str(code).zfill(6),
        "name": data.name_for(str(code).zfill(6)),
        "is_demo": is_demo,
        "quote": {
            "close": round(float(last["close"]), 2),
            "change": round(change, 2),
            "pct": round(pct, 2),
            "high": round(float(last["high"]), 2),
            "low": round(float(last["low"]), 2),
            "volume": int(last["volume"]),
            "date": str(last["date"]),
        },
        "kline": {
            "dates": list(df["date"]),
            "ohlc": [
                [round(float(o), 2), round(float(c), 2), round(float(l), 2), round(float(h), 2)]
                for o, c, l, h in zip(df["open"], df["close"], df["low"], df["high"])
            ],
            "volume": [int(v) for v in df["volume"]],
            "ma5": _series(df, "ma5"),
            "ma20": _series(df, "ma20"),
            "ma60": _series(df, "ma60"),
            "boll_upper": _series(df, "boll_upper"),
            "boll_lower": _series(df, "boll_lower"),
            "dif": _series(df, "dif"),
            "dea": _series(df, "dea"),
            "macd": _series(df, "macd"),
            "rsi12": _series(df, "rsi12"),
        },
        "analysis": analysis,
        "trends": trends,
    }


@app.get("/api/stock/{code}")
def stock(code: str, days: int = Query(160, ge=30, le=500), adjust: str = "qfq") -> dict:
    return _build_stock_payload(code, days, adjust)


@app.get("/api/stock/{code}/fundamentals")
def stock_fundamentals(code: str) -> dict:
    return {"code": str(code).zfill(6), "name": data.name_for(str(code).zfill(6)),
            **data.get_fundamentals(code)}


def _build_plan(code: str, days: int = 160, adjust: str = "qfq") -> dict:
    """规则化「投资大师」操作计划（买入区/止盈/止损/总体倾向），不调用 LLM。"""
    df, is_demo = data.get_kline(code, days=days, adjust=adjust)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="未取到该股票数据")
    df = compute_all(df)
    trends = trend_overview(df)
    funda = data.get_fundamentals(code)
    plan = strategies.build_plan(df, trends, funda)
    return {"code": str(code).zfill(6), "name": data.name_for(str(code).zfill(6)),
            "is_demo": is_demo, "plan": plan}


@app.get("/api/stock/{code}/plan")
def stock_plan(code: str, days: int = Query(160, ge=30, le=500), adjust: str = "qfq") -> dict:
    return _build_plan(code, days, adjust)


@app.get("/api/backtest/strategies")
def backtest_strategies() -> dict:
    return backtest.list_strategies()


@app.get("/api/stock/{code}/backtest")
def stock_backtest(code: str, strategy: str = Query("composite"),
                   days: int = Query(250, ge=60, le=1000), adjust: str = "qfq") -> dict:
    return backtest.run(code, strategy=strategy, days=days, adjust=adjust)


def _compact(code: str, days: int = 120) -> dict:
    p = _build_stock_payload(code, days, "qfq")
    a = p["analysis"]
    return {
        "code": p["code"], "name": p["name"], "is_demo": p["is_demo"],
        "close": p["quote"]["close"], "pct": p["quote"]["pct"],
        "verdict": a["verdict"], "verdict_tone": a["verdict_tone"], "counts": a["counts"],
        "signals": [{"dim": s["dim"], "tone": s["tone"], "label": s["label"]} for s in a["signals"]],
    }


@app.get("/api/market")
def market() -> dict:
    """大盘指数 + 全市场涨跌家数。"""
    return data.get_index_overview()


# ---- 情报：板块热度 + 财经快讯 + 个股新闻（Phase 1）----
@app.get("/api/sectors")
def sectors(kind: str = Query("industry")) -> dict:
    return intel.get_sector_board(kind)


@app.get("/api/news")
def news(limit: int = Query(40, ge=5, le=100)) -> dict:
    return intel.get_global_news(limit)


@app.get("/api/stock/{code}/news")
def stock_news(code: str, limit: int = Query(10, ge=1, le=30)) -> dict:
    return intel.get_stock_news(code, limit)


# ---- 情报 Phase 2：LLM 情绪打标 + 板块情绪榜 + 个股消息面（按需，消耗 token）----
@app.post("/api/news/sentiment")
def news_sentiment(limit: int = Query(40, ge=10, le=100),
                   cap: int = Query(24, ge=5, le=40)) -> dict:
    try:
        return intel.analyze_global_sentiment(limit, cap)
    except llm.LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/stock/{code}/intel")
def stock_intel(code: str, limit: int = Query(8, ge=3, le=20),
                pref: str = Query("balanced")) -> dict:
    try:
        return intel.analyze_stock_sentiment(code, limit, pref)
    except llm.LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _safe_compact(code: str) -> dict:
    try:
        return _compact(code)
    except Exception as exc:
        return {"code": code, "name": data.name_for(code), "error": str(exc)[:60]}


@app.get("/api/overview")
def overview() -> dict:
    codes = storage.load()
    items = list(_EXEC.map(_safe_compact, codes)) if codes else []
    return {"items": items}


# 扫描规则：基于规则引擎信号判定
_RULES = {
    "ma_bull": ("均线多头排列", lambda s: any(x["dim"] == "趋势" and x["tone"] == "bullish" for x in s)),
    "macd_golden": ("MACD 金叉", lambda s: any(x["dim"] == "MACD" and "金叉" in x["label"] for x in s)),
    "rsi_oversold": ("RSI 超卖", lambda s: any(x["dim"] == "RSI" and "超卖" in x["label"] for x in s)),
    "rsi_overbought": ("RSI 超买", lambda s: any(x["dim"] == "RSI" and "超买" in x["label"] for x in s)),
    "vol_surge": ("放量", lambda s: any(x["dim"] == "量能" and "放量" in x["label"] for x in s)),
    "near_lower": ("逼近布林下轨", lambda s: any(x["dim"] == "布林带" and "下轨" in x["label"] for x in s)),
}


@app.get("/api/scan/rules")
def scan_rules() -> dict:
    return {"rules": [{"id": k, "label": v[0]} for k, v in _RULES.items()]}


# 全市场扫描成本高（逐只取K线）：按池上限，防止单次请求过久
_SCAN_CAP = {"watchlist": 200, "sz50": 50, "hs300": 120}


def _scan_codes(scope: str, payload: dict) -> tuple[list[str], int]:
    """返回 (待扫描代码, 池总数)。scope: watchlist / sz50 / hs300。"""
    if scope == "sz50":
        pool = data.get_pool_codes("sz50")
    elif scope == "hs300":
        pool = data.get_pool_codes("hs300")
    else:
        scope = "watchlist"
        pool = payload.get("codes") or storage.load()
    total = len(pool)
    cap = int(payload.get("limit") or _SCAN_CAP.get(scope, 80))
    return pool[:cap], total


def _scan_eval(code: str, scope: str, rule_ids: list[str]):
    """评估单只股票是否命中全部规则。返回 (status, payload)。"""
    try:
        c = _compact(code)
        if scope != "watchlist" and c.get("is_demo"):  # 全市场扫描跳过取不到真实数据的
            return ("fail", None)
        hit = [rid for rid in rule_ids if _RULES[rid][1](c["signals"])]
        if len(hit) == len(rule_ids):  # 需同时满足所有所选规则
            c["matched"] = [_RULES[r][0] for r in hit]
            return ("hit", c)
        return ("miss", None)
    except Exception:
        return ("fail", None)


def _prepare_scan(payload: dict) -> tuple[list[str], str, list[str], int]:
    rule_ids = [r for r in payload.get("rules", []) if r in _RULES]
    if not rule_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个筛选规则")
    scope = (payload.get("scope") or "watchlist").lower()
    codes, pool_total = _scan_codes(scope, payload)
    if not codes:
        raise HTTPException(status_code=400, detail="股票池为空（自选股请先添加，或换其他范围）")
    return rule_ids, scope, codes, pool_total


@app.post("/api/scan")
def scan(payload: dict = Body(...)) -> dict:
    rule_ids, scope, codes, pool_total = _prepare_scan(payload)
    matches, failed = [], 0
    for status, c in _EXEC.map(lambda code: _scan_eval(code, scope, rule_ids), codes):
        if status == "hit":
            matches.append(c)
        elif status == "fail":
            failed += 1
    db.maybe_cleanup(protect=set(storage.load()))  # 扫描会新增很多股票，顺便触发清理
    return {"count": len(matches), "scope": scope, "scanned": len(codes),
            "pool_total": pool_total, "failed": failed, "items": matches}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/scan/stream")
def scan_stream(rules: str = Query(""), scope: str = Query("watchlist"),
                limit: int | None = Query(None)):
    """流式扫描：边扫边把命中的股票通过 SSE 推给前端。"""
    payload = {"rules": [r for r in rules.split(",") if r], "scope": scope}
    if limit:
        payload["limit"] = limit
    rule_ids, scope, codes, pool_total = _prepare_scan(payload)

    def gen():
        yield _sse("meta", {"scope": scope, "pool_total": pool_total, "scanned": len(codes)})
        count = failed = done = 0
        futures = {_EXEC.submit(_scan_eval, code, scope, rule_ids): code for code in codes}
        for fut in as_completed(futures):
            status, c = fut.result()
            done += 1
            if status == "hit":
                count += 1
                yield _sse("hit", c)
            elif status == "fail":
                failed += 1
            yield _sse("progress", {"done": done, "total": len(codes)})
        db.maybe_cleanup(protect=set(storage.load()))
        yield _sse("done", {"count": count, "failed": failed,
                            "scanned": len(codes), "pool_total": pool_total, "scope": scope})

    return StreamingResponse(gen(), media_type="text/event-stream")


_GEO_PREFIX = {
    "贵州", "北京", "上海", "深圳", "广东", "浙江", "江苏", "山东", "福建", "四川",
    "湖南", "湖北", "河南", "河北", "安徽", "江西", "陕西", "山西", "云南", "广西",
    "重庆", "天津", "辽宁", "吉林", "新疆", "西藏", "甘肃", "青海", "宁夏", "海南", "中国",
}

# 去前缀后若剩下这些通用词，则不作为简称匹配（避免“银行/科技”等误命中）
_GENERIC_REMAINDER = {
    "银行", "证券", "科技", "股份", "集团", "控股", "实业", "发展", "国际", "电子",
    "能源", "医药", "生物", "材料", "信息", "电力", "环保", "文化", "传媒", "高速",
    "机场", "港口", "高新", "通信", "汽车", "机械", "化工", "食品", "地产", "建设",
}


def _extract_stocks(question: str, limit: int = 3) -> list[str]:
    """从问题中识别股票：6 位代码 + 完整名称 + 去地名前缀的简称（贵州茅台→茅台）。

    采用高精度策略，避免跨词误匹配；匹配不到时由 LLM 提示用户用全称/代码。
    """
    import re

    found: list[tuple[int, str]] = []  # (优先级, code)，数字越大越优先
    for c in re.findall(r"\d{6}", question):
        found.append((3, c))
    try:
        df = data._stock_list()
        for name, code in zip(df["name"].astype(str), df["code"].astype(str)):
            if name in question:  # 完整名称，最可靠
                found.append((2, code))
            elif (len(name) >= 4 and name[:2] in _GEO_PREFIX
                  and name[2:] not in _GENERIC_REMAINDER and name[2:] in question):
                found.append((1, code))  # 去掉省份/地名后的简称
    except Exception:
        pass

    found.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, c in found:
        c = str(c).zfill(6)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:limit]


_CONTEXT_LABELS = {"market": "大盘总览", "watchlist": "自选股技术面", "intel": "情报快讯"}


def _ctx_market() -> str:
    mk = data.get_index_overview()
    idx = "；".join(
        f"{i['name']} {i['price']}（{i['pct']:+.2f}%）" for i in (mk.get("indices") or [])
    )
    b = mk.get("breadth") or {}
    breadth = ""
    if b.get("up") is not None:
        breadth = f"；全市场涨跌家数 涨{b.get('up')}/跌{b.get('down')}，涨停{b.get('limit_up')}/跌停{b.get('limit_down')}"
    return (idx + breadth) if idx else ""


def _ctx_watchlist() -> str:
    codes = storage.load()
    if not codes:
        return "（自选股为空）"
    items = list(_EXEC.map(_safe_compact, codes))
    lines = []
    for c in items:
        if c.get("error"):
            continue
        lines.append(f"{c['name']}({c['code']}) 收盘{c.get('close')}（{c.get('pct')}%）综合判断：{c.get('verdict')}")
    return "；".join(lines)


def _ctx_intel() -> str:
    parts = []
    try:
        sec = intel.get_sector_board("industry")
        lead = "、".join(f"{s['name']}({s['pct']:+.2f}%)" for s in (sec.get("leaders") or [])[:6])
        lag = "、".join(f"{s['name']}({s['pct']:+.2f}%)" for s in (sec.get("laggards") or [])[:4])
        if lead:
            parts.append(f"板块领涨：{lead}")
        if lag:
            parts.append(f"板块领跌：{lag}")
    except Exception:
        pass
    try:
        news = intel.get_global_news(30)
        rows = []
        for n in (news.get("items") or [])[:12]:
            sent = n.get("sentiment")
            tag = f"[{sent}]" if sent and sent != "中性" else ""
            rows.append(f"{tag}{n.get('title', '')}")
        if rows:
            parts.append("近期快讯：" + "；".join(rows))
    except Exception:
        pass
    return "\n".join(parts)


_CTX_BUILDERS = {"market": _ctx_market, "watchlist": _ctx_watchlist, "intel": _ctx_intel}


def _build_context_blocks(sources: list) -> list[dict]:
    blocks = []
    for src in sources or []:
        fn = _CTX_BUILDERS.get(src)
        if not fn:
            continue
        try:
            text = (fn() or "").strip()
        except Exception:
            text = ""
        if text:
            blocks.append({"label": _CONTEXT_LABELS.get(src, src), "text": text})
    return blocks


@app.post("/api/chat")
def chat(payload: dict = Body(...)) -> dict:
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    codes = _extract_stocks(question)
    contexts = []
    for code in codes:
        try:
            contexts.append(_build_stock_payload(code, 120, "qfq"))
        except Exception:
            continue
    extras = _build_context_blocks(payload.get("sources") or [])
    try:
        text = llm.chat(llm.build_chat_messages(question, contexts, extras))
    except llm.LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "text": text,
        "used": [{"code": c["code"], "name": c["name"]} for c in contexts],
        "sources_used": [b["label"] for b in extras],
    }


# ---- 每日复盘 / 晨报 ----
def _digest_alerts(items: list[dict]) -> list[dict]:
    """从自选股技术信号派生『今日提示』（纯规则、零成本）。"""
    alerts = []
    for c in items:
        if c.get("error"):
            continue
        name, code, pct = c.get("name"), c.get("code"), c.get("pct")
        if pct is not None and abs(pct) >= 5:
            alerts.append({"code": code, "tone": "bullish" if pct > 0 else "bearish",
                           "text": f"{name} 今日{'大涨' if pct > 0 else '大跌'} {pct}%，注意波动"})
        for s in c.get("signals", []):
            dim, lab = s.get("dim"), s.get("label", "")
            if dim == "RSI" and "超买" in lab:
                alerts.append({"code": code, "tone": "warning", "text": f"{name} {lab}，短期追高风险偏大"})
            elif dim == "RSI" and "超卖" in lab:
                alerts.append({"code": code, "tone": "bullish", "text": f"{name} {lab}，关注超跌反弹机会"})
            elif dim == "MACD" and "金叉" in lab:
                alerts.append({"code": code, "tone": "bullish", "text": f"{name} MACD {lab}，动能转强"})
            elif dim == "MACD" and "死叉" in lab:
                alerts.append({"code": code, "tone": "bearish", "text": f"{name} MACD {lab}，动能转弱"})
        if c.get("verdict_tone") == "bearish":
            alerts.append({"code": code, "tone": "bearish", "text": f"{name} 技术面：{c.get('verdict')}"})
    return alerts


def _news_alerts(items: list[dict], news: list[dict]) -> list[dict]:
    """新闻预警：自选股名字出现在『利好/利空』快讯标题里就提醒（用已缓存情绪，零额外成本）。"""
    names = [(c["code"], c["name"]) for c in items if c.get("name") and len(c["name"]) >= 2]
    alerts, seen = [], set()
    for n in news:
        sent = n.get("sentiment")
        if sent not in ("利好", "利空"):
            continue
        title = n.get("title", "")
        for code, name in names:
            key = (code, title)
            if name in title and key not in seen:
                seen.add(key)
                alerts.append({"code": code, "tone": "bullish" if sent == "利好" else "bearish",
                               "text": f"{name} 相关{sent}消息：{title}"})
    return alerts


_TONE_PRIORITY = {"bearish": 0, "warning": 1, "bullish": 2, "neutral": 3}


@app.get("/api/digest")
def digest() -> dict:
    """每日复盘结构化数据（纯规则、免费）：大盘 + 自选股体检 + 提示 + 板块/快讯。"""
    codes = storage.load()
    items = list(_EXEC.map(_safe_compact, codes)) if codes else []
    valid = [c for c in items if not c.get("error")]
    movers = sorted(valid, key=lambda c: (c.get("pct") if c.get("pct") is not None else 0))
    try:
        mk = data.get_index_overview()
    except Exception:
        mk = {"indices": [], "breadth": {}}
    try:
        sec = intel.get_sector_board("industry")
    except Exception:
        sec = {"leaders": [], "laggards": []}
    try:
        news = intel.get_global_news(20)
        news_items = news.get("items") or []
    except Exception:
        news_items = []
    alerts = _digest_alerts(valid) + _news_alerts(valid, news_items)
    alerts.sort(key=lambda a: _TONE_PRIORITY.get(a.get("tone"), 9))
    return {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market": {"indices": mk.get("indices", []), "breadth": mk.get("breadth", {}),
                   "is_demo": mk.get("is_demo", False)},
        "watchlist": valid,
        "top_up": list(reversed(movers))[:3],
        "top_down": movers[:3],
        "alerts": alerts,
        "alert_count": len(alerts),
        "risk_count": sum(1 for a in alerts if a.get("tone") in ("bearish", "warning")),
        "sectors": {"leaders": (sec.get("leaders") or [])[:6], "laggards": (sec.get("laggards") or [])[:4]},
        "news": news_items[:8],
    }


@app.post("/api/digest/ai/stream")
def digest_ai_stream(pref: str = Query("balanced")):
    """用 LLM 把今日大盘/自选股/情报写成一份大白话复盘（流式，消耗 token）。"""
    blocks = _build_context_blocks(["market", "watchlist", "intel"])

    def gen():
        try:
            for piece in llm.chat_stream(llm.build_digest_messages(blocks, pref)):
                yield piece
        except llm.LLMError as exc:
            yield f"\n[错误] {exc}"

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


# ---- 持仓与盈亏 ----
def _position_note(pnl_pct: float, verdict_tone: str) -> dict:
    """根据浮盈浮亏 + 技术倾向给出『提示』（非投资建议）。"""
    profit = pnl_pct >= 0
    if verdict_tone == "bearish":
        if profit:
            return {"tone": "warning", "text": "技术面转弱、当前有浮盈：可考虑分批止盈、落袋为安。"}
        return {"tone": "bearish", "text": "技术面转弱且浮亏：注意风险、设好止损，避免越亏越补。"}
    if verdict_tone == "bullish":
        if profit:
            return {"tone": "bullish", "text": "趋势仍偏强、浮盈中：可继续持有或上移止盈位锁定利润。"}
        return {"tone": "neutral", "text": "趋势偏强但你成本偏高：可观察企稳信号，不必急于补仓。"}
    if verdict_tone == "warning":
        return {"tone": "warning", "text": "信号矛盾/高位震荡：控制仓位，不宜追加。"}
    return {"tone": "neutral", "text": "方向不明：按计划持有，等更清晰的信号。"}


def _eval_position(pos: dict) -> dict:
    code = str(pos.get("code", "")).zfill(6)
    shares = float(pos.get("shares") or 0)
    cost = float(pos.get("cost") or 0)
    out = {"code": code, "name": data.name_for(code), "shares": shares, "cost": cost,
           "note_user": pos.get("note", "")}
    try:
        c = _compact(code)
        price = c["close"]
        out.update({"price": price, "verdict": c["verdict"], "verdict_tone": c["verdict_tone"]})
    except Exception as exc:
        out.update({"price": None, "error": str(exc)[:60]})
        return out
    mv = price * shares
    cv = cost * shares
    pnl = mv - cv
    pnl_pct = (price / cost - 1) * 100 if cost > 0 else 0.0
    out.update({
        "market_value": round(mv, 2), "cost_value": round(cv, 2),
        "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
        "tip": _position_note(pnl_pct, c["verdict_tone"]),
    })
    return out


@app.get("/api/portfolio")
def get_portfolio() -> dict:
    positions = storage.load_positions()
    items = list(_EXEC.map(_eval_position, positions)) if positions else []
    valid = [it for it in items if it.get("price") is not None]
    total_mv = sum(it["market_value"] for it in valid)
    total_cv = sum(it["cost_value"] for it in valid)
    total_pnl = total_mv - total_cv
    for it in valid:  # 仓位占比（按市值）
        it["weight"] = round(it["market_value"] / total_mv * 100, 1) if total_mv else 0.0
    return {
        "items": items,
        "summary": {
            "market_value": round(total_mv, 2), "cost_value": round(total_cv, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round((total_mv / total_cv - 1) * 100, 2) if total_cv else 0.0,
            "count": len(valid),
        },
    }


@app.post("/api/portfolio")
def upsert_portfolio(payload: dict = Body(...)) -> dict:
    code = str(payload.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="股票代码不能为空")
    try:
        shares = float(payload.get("shares"))
        cost = float(payload.get("cost"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="数量与成本价必须是数字")
    if shares <= 0 or cost <= 0:
        raise HTTPException(status_code=400, detail="数量与成本价必须大于 0")
    storage.upsert_position(code, shares, cost, str(payload.get("note") or ""))
    return get_portfolio()


@app.delete("/api/portfolio/{code}")
def delete_portfolio(code: str) -> dict:
    storage.remove_position(code)
    return get_portfolio()


# ---- LLM 接入：多档配置管理 + 切换 ----
@app.get("/api/llm/config")
def llm_get_config() -> dict:
    return llm.public_config()


@app.get("/api/llm/profiles")
def llm_list_profiles() -> dict:
    return llm.list_profiles()


@app.post("/api/llm/profiles")
def llm_upsert_profile(payload: dict = Body(...)) -> dict:
    return llm.upsert_profile(
        pid=payload.get("id"),
        name=payload.get("name"),
        base_url=payload.get("base_url"),
        api_key=payload.get("api_key"),
        model=payload.get("model"),
        proxy=payload.get("proxy"),
    )


@app.delete("/api/llm/profiles/{pid}")
def llm_delete_profile(pid: str) -> dict:
    return llm.delete_profile(pid)


@app.post("/api/llm/active")
def llm_set_active(payload: dict = Body(...)) -> dict:
    pid = payload.get("id")
    if not pid:
        raise HTTPException(status_code=400, detail="缺少 id")
    return llm.set_active(pid)


@app.post("/api/stock/{code}/ai")
def stock_ai(code: str, days: int = Query(160, ge=30, le=500), adjust: str = "qfq",
             pref: str = Query("balanced")) -> dict:
    payload = _build_stock_payload(code, days, adjust)
    funda = data.get_fundamentals(code)
    plan = _build_plan(code, days, adjust)["plan"]
    try:
        text = llm.chat(llm.build_messages(payload, funda, plan, pref))
    except llm.LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"code": payload["code"], "name": payload["name"], "model": llm.get_config()["model"], "text": text}


@app.post("/api/stock/{code}/ai/stream")
def stock_ai_stream(code: str, days: int = Query(160, ge=30, le=500), adjust: str = "qfq",
                    pref: str = Query("balanced")):
    payload = _build_stock_payload(code, days, adjust)
    funda = data.get_fundamentals(code)
    plan = _build_plan(code, days, adjust)["plan"]

    def gen():
        try:
            for piece in llm.chat_stream(llm.build_messages(payload, funda, plan, pref)):
                yield piece
        except llm.LLMError as exc:
            yield f"\n[错误] {exc}"

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


@app.get("/api/masters")
def masters() -> dict:
    """可单独调用 AI 解读的投资大师列表（skill 清单）。"""
    return {"masters": [{"key": k, "speaker": v["speaker"]}
                        for k, v in llm.MASTER_PERSONA.items()]}


@app.post("/api/stock/{code}/master/{key}/ai/stream")
def stock_master_ai_stream(code: str, key: str,
                           days: int = Query(160, ge=30, le=500), adjust: str = "qfq"):
    """单个大师 skill：以该大师口吻流式输出买卖解读（仅此调用才消耗 token）。"""
    if key not in llm.MASTER_PERSONA:
        raise HTTPException(status_code=404, detail="未知的大师")
    payload = _build_stock_payload(code, days, adjust)
    funda = data.get_fundamentals(code)
    plan = _build_plan(code, days, adjust)["plan"]

    def gen():
        try:
            for piece in llm.chat_stream(llm.build_master_messages(payload, funda, plan, key)):
                yield piece
        except llm.LLMError as exc:
            yield f"\n[错误] {exc}"

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


# ---- 前端静态托管（放在最后，避免覆盖 /api 路由）----
if FRONTEND_DIR.exists():
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
