"""LLM 接入层：OpenAI 兼容协议，支持接入你自己的大模型。

兼容 OpenAI / DeepSeek / Kimi(Moonshot) / 通义 / OpenRouter / 本地 Ollama 等，
只要对方提供 `{base_url}/chat/completions` 接口即可。

配置优先级：运行时网页配置(data/llm_config.json) > 环境变量。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import requests

try:  # 可选：支持项目根目录的 .env 文件
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
_CONFIG_FILE = _DATA_DIR / "llm_config.json"

_DEFAULT_BASE = "https://api.openai.com/v1"


class LLMError(Exception):
    pass


def _mask(key: str) -> str:
    return (key[:3] + "****" + key[-4:]) if len(key) > 8 else ("****" if key else "")


def _load() -> dict:
    """读取多档配置，兼容旧的单档格式并自动迁移。"""
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict) and "profiles" in data:
            return data
        # 迁移旧的扁平格式 {base_url, api_key, model}，并落盘固化 id
        if isinstance(data, dict) and (data.get("api_key") or data.get("base_url")):
            pid = uuid.uuid4().hex[:8]
            migrated = {"active": pid, "profiles": {pid: {
                "name": "默认", "base_url": data.get("base_url") or _DEFAULT_BASE,
                "api_key": data.get("api_key", ""), "model": data.get("model") or "gpt-4o-mini",
            }}}
            _save(migrated)
            return migrated
    return {"active": None, "profiles": {}}


def _save(data: dict) -> None:
    _CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_profiles() -> dict:
    """所有配置档（隐藏 key）+ 当前激活档。"""
    data = _load()
    profiles = [
        {
            "id": pid, "name": p.get("name") or pid,
            "base_url": p.get("base_url", ""), "model": p.get("model", ""),
            "api_key_masked": _mask(p.get("api_key", "")), "configured": bool(p.get("api_key")),
        }
        for pid, p in data["profiles"].items()
    ]
    return {"active": data.get("active"), "profiles": profiles}


def upsert_profile(pid: str | None, name: str | None, base_url: str | None,
                   api_key: str | None, model: str | None) -> dict:
    data = _load()
    if not pid:
        pid = uuid.uuid4().hex[:8]
    existing = data["profiles"].get(pid, {})
    prof = {
        "name": (name or existing.get("name") or "未命名").strip(),
        "base_url": (base_url or existing.get("base_url") or _DEFAULT_BASE).strip().rstrip("/"),
        "model": (model or existing.get("model") or "gpt-4o-mini").strip(),
        "api_key": existing.get("api_key", ""),
    }
    if api_key and api_key.strip() and api_key.strip() != "****":
        prof["api_key"] = api_key.strip()
    data["profiles"][pid] = prof
    if not data.get("active"):
        data["active"] = pid
    _save(data)
    return list_profiles()


def delete_profile(pid: str) -> dict:
    data = _load()
    data["profiles"].pop(pid, None)
    if data.get("active") == pid:
        data["active"] = next(iter(data["profiles"]), None)
    _save(data)
    return list_profiles()


def set_active(pid: str) -> dict:
    data = _load()
    if pid in data["profiles"]:
        data["active"] = pid
        _save(data)
    return list_profiles()


def get_config() -> dict:
    """返回当前生效配置（激活档 > 环境变量 > 默认）。"""
    data = _load()
    pid = data.get("active")
    p = data["profiles"].get(pid) if pid else None
    if p and p.get("api_key"):
        return {"base_url": p["base_url"], "api_key": p["api_key"], "model": p["model"]}
    return {
        "base_url": (p or {}).get("base_url") or os.getenv("LLM_BASE_URL") or _DEFAULT_BASE,
        "api_key": (p or {}).get("api_key") or os.getenv("LLM_API_KEY") or "",
        "model": (p or {}).get("model") or os.getenv("LLM_MODEL") or "gpt-4o-mini",
    }


def public_config() -> dict:
    """当前激活配置摘要（供页头显示）。"""
    cfg = get_config()
    return {
        "configured": bool(cfg["api_key"]),
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "api_key_masked": _mask(cfg["api_key"]),
    }


def chat(messages: list[dict], temperature: float = 0.4, max_tokens: int = 900) -> str:
    cfg = get_config()
    if not cfg["api_key"]:
        raise LLMError("尚未配置 LLM：请在网页右上角「AI 设置」填写 base_url / api_key / model。")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg["model"],
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=90,
        )
    except requests.RequestException as exc:
        raise LLMError(f"调用 LLM 失败（网络/地址错误）：{exc}") from exc

    if resp.status_code != 200:
        raise LLMError(f"LLM 返回错误 {resp.status_code}：{resp.text[:200]}")
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"无法解析 LLM 响应：{resp.text[:200]}") from exc


def chat_stream(messages: list[dict], temperature: float = 0.4, max_tokens: int = 900):
    """流式生成：逐块 yield 文本（OpenAI 兼容 SSE）。"""
    cfg = get_config()
    if not cfg["api_key"]:
        raise LLMError("尚未配置 LLM：请在网页左下「⚙ AI」填写 base_url / api_key / model。")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": cfg["model"], "messages": messages, "temperature": temperature,
                  "max_tokens": max_tokens, "stream": True},
            timeout=90,
            stream=True,
        )
    except requests.RequestException as exc:
        raise LLMError(f"调用 LLM 失败（网络/地址错误）：{exc}") from exc

    if resp.status_code != 200:
        raise LLMError(f"LLM 返回错误 {resp.status_code}：{resp.text[:200]}")

    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", errors="ignore").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            delta = json.loads(payload)["choices"][0].get("delta", {})
            piece = delta.get("content")
            if piece:
                yield piece
        except (KeyError, IndexError, ValueError):
            continue


def build_chat_messages(question: str, contexts: list[dict]) -> list[dict]:
    """自然语言问答：把问题与相关个股数据打包。"""
    system = (
        "你是一名严谨客观的 A 股投研助手，面向新手用通俗中文回答。"
        "只能基于下方提供的数据作答，不要编造数字；涉及指标要解释含义并指出矛盾；"
        "必须提示风险、强调不是投资建议、技术指标有滞后性。"
        "不要给出『一定涨跌』『满仓梭哈』之类的话。若数据不足以回答，请直说。"
    )
    if contexts:
        ctx = "\n\n".join(
            f"【{c['name']}（{c['code']}）】最新收盘 {c['quote']['close']}（{c['quote']['pct']}%），"
            f"综合判断：{c['analysis']['verdict']}；信号："
            + "；".join(f"{s['dim']}={s['label']}" for s in c["analysis"]["signals"])
            for c in contexts
        )
    else:
        ctx = "（未识别到具体股票，请基于通用投资常识回答，并建议用户指明股票代码/名称。）"
    user = f"相关数据：\n{ctx}\n\n用户问题：{question}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _trends_text(stock: dict) -> str:
    t = stock.get("trends") or {}
    items = t.get("items") or {}
    if not items:
        return ""
    parts = "；".join(f"{v['name']}{v['label']}" for v in items.values())
    align = (t.get("align") or {}).get("label", "")
    return f"多周期趋势：{parts}（{align}）"


def _valuation_text(funda: dict | None) -> str:
    if not funda:
        return ""
    v = funda.get("valuation") or {}
    bits = []
    for key, name in (("pe_ttm", "PE(TTM)"), ("pb", "PB")):
        if v.get(key) is not None:
            pct = v.get(key + "_pct")
            if pct is not None:
                level = "偏低/便宜" if pct <= 30 else ("偏高/偏贵" if pct >= 70 else "中等")
                bits.append(f"{name} {v[key]}（近五年 {pct}% 分位，{level}）")
            else:
                bits.append(f"{name} {v[key]}")
    ff = funda.get("fund_flow") or {}
    if ff.get("main_net") is not None:
        bits.append(f"主力净流入 {ff['main_net']} 万（{ff.get('date','')}）")
    return "估值/资金：" + "；".join(bits) if bits else ""


_PREF_GUIDE = {
    "short": ("短线收益（数周内）",
              "以【趋势跟随】【超跌反弹】和动量为主，价位要贴近现价、止盈止损都要紧；"
              "估值/成长仅作背景。强调择时与纪律：买点、第一止盈、止损三个价位必须明确。"),
    "long": ("长期持有（数月至数年）",
             "以【价值/逆向】【成长匹配】和估值分位为主，淡化短期波动；"
             "强调分批建仓区间、长期持有的加仓/减仓条件，止损放宽，不被日线噪音震出。"),
    "balanced": ("均衡",
                 "兼顾趋势择时与估值安全边际，给出适合中线的买入区间与止盈止损。"),
}


def _plan_text(plan: dict | None, pref: str) -> str:
    if not plan:
        return ""
    try:
        from .strategies import plan_text
        return plan_text(plan, pref)
    except Exception:
        return ""


def build_messages(stock: dict, funda: dict | None = None,
                   plan: dict | None = None, pref: str = "balanced") -> list[dict]:
    """把行情/指标/多周期趋势/估值分位/大师买卖纪律打包成给 LLM 的中文提示。

    pref：short=短线收益 / long=长期持有 / balanced=均衡，影响大师视角与价位侧重。
    """
    q = stock["quote"]
    a = stock["analysis"]
    k = stock["kline"]
    signals_text = "\n".join(f"- {s['dim']}：{s['label']} —— {s['text']}" for s in a["signals"])
    latest = {
        "收盘": q["close"], "涨跌幅%": q["pct"], "日期": q["date"],
        "MA5": k["ma5"][-1], "MA20": k["ma20"][-1], "MA60": k["ma60"][-1],
        "DIF": k["dif"][-1], "DEA": k["dea"][-1], "MACD柱": k["macd"][-1],
        "RSI12": k["rsi12"][-1],
        "布林上轨": k["boll_upper"][-1], "布林下轨": k["boll_lower"][-1],
    }
    pref_name, pref_rule = _PREF_GUIDE.get(pref, _PREF_GUIDE["balanced"])
    system = (
        "你是一名严谨、客观的 A 股投研助手，擅长用投资大师的纪律给出『买卖位置与条件』，面向新手用通俗中文。"
        "请只基于下方提供的数据，不要编造数字；技术指标有滞后性、数据为日线级别非实时。\n"
        f"用户偏好：{pref_name}。{pref_rule}\n"
        "下方『大师买卖纪律』已由规则引擎算好具体价位，请直接采用这些数字，必要时解释为什么。\n"
        "你不能预测『几月几号买』，只能给『在什么位置、满足什么条件就买/卖』。\n"
        "输出结构：\n"
        "1) 一句话总体判断（结合用户偏好）；\n"
        "2) 投资大师怎么看：【价值/逆向(格雷厄姆·巴菲特)】【成长(彼得林奇·PEG)】【趋势(顺势)】"
        "【超跌反弹(短线)】各 1-2 句，数据不足就直说；\n"
        "3) 【操作计划】用要点给出：① 买入区间/触发条件 ② 第一止盈位/减仓条件 ③ 止损位 "
        "（直接引用下方算好的价位，按用户偏好排序侧重）；\n"
        "4) 情景化前瞻：分『偏多情景』『偏空情景』，各说明需要满足什么条件/突破或跌破哪个价位才成立；\n"
        "5) 风险提示：强调这不是投资建议、指标滞后、便宜可能更便宜/趋势可能反转、应带止损并先用模拟盘验证。\n"
        "不要给出『一定涨/一定跌』『满仓/梭哈』之类的话。"
    )
    extra = "\n".join(x for x in (_trends_text(stock), _valuation_text(funda),
                                  _plan_text(plan, pref)) if x)
    user = (
        f"股票：{stock['name']}（{stock['code']}）\n"
        f"最新数据：{json.dumps(latest, ensure_ascii=False)}\n"
        f"{extra}\n\n"
        f"规则引擎给出的综合判断：{a['verdict']}\n"
        f"各维度信号：\n{signals_text}\n\n"
        "请基于以上数据，按上述结构给出中文深度点评、可执行的买卖计划与情景化前瞻。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
