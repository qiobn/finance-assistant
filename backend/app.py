"""FastAPI 应用：提供数据/分析 API，并托管前端静态页面。

启动：
    uvicorn backend.app:app --reload --port 8000
然后浏览器打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import data, db, llm, storage
from .analysis import build_analysis
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
    }


@app.get("/api/stock/{code}")
def stock(code: str, days: int = Query(160, ge=30, le=500), adjust: str = "qfq") -> dict:
    return _build_stock_payload(code, days, adjust)


@app.get("/api/stock/{code}/fundamentals")
def stock_fundamentals(code: str) -> dict:
    return {"code": str(code).zfill(6), "name": data.name_for(str(code).zfill(6)),
            **data.get_fundamentals(code)}


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
    try:
        text = llm.chat(llm.build_chat_messages(question, contexts))
    except llm.LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"text": text, "used": [{"code": c["code"], "name": c["name"]} for c in contexts]}


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
def stock_ai(code: str, days: int = Query(160, ge=30, le=500), adjust: str = "qfq") -> dict:
    payload = _build_stock_payload(code, days, adjust)
    try:
        text = llm.chat(llm.build_messages(payload))
    except llm.LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"code": payload["code"], "name": payload["name"], "model": llm.get_config()["model"], "text": text}


@app.post("/api/stock/{code}/ai/stream")
def stock_ai_stream(code: str, days: int = Query(160, ge=30, le=500), adjust: str = "qfq"):
    payload = _build_stock_payload(code, days, adjust)

    def gen():
        try:
            for piece in llm.chat_stream(llm.build_messages(payload)):
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
