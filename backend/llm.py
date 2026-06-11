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
            "proxy": p.get("proxy", ""),
            "api_key_masked": _mask(p.get("api_key", "")), "configured": bool(p.get("api_key")),
        }
        for pid, p in data["profiles"].items()
    ]
    return {"active": data.get("active"), "profiles": profiles}


def upsert_profile(pid: str | None, name: str | None, base_url: str | None,
                   api_key: str | None, model: str | None,
                   proxy: str | None = None) -> dict:
    data = _load()
    if not pid:
        pid = uuid.uuid4().hex[:8]
    existing = data["profiles"].get(pid, {})
    prof = {
        "name": (name or existing.get("name") or "未命名").strip(),
        "base_url": (base_url or existing.get("base_url") or _DEFAULT_BASE).strip().rstrip("/"),
        "model": (model or existing.get("model") or "gpt-4o-mini").strip(),
        "api_key": existing.get("api_key", ""),
        # 代理：留空=直连（默认忽略系统代理）；需要时填 http(s)://host:port
        "proxy": (proxy if proxy is not None else existing.get("proxy", "") or "").strip(),
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
        return {"base_url": p["base_url"], "api_key": p["api_key"], "model": p["model"],
                "proxy": (p.get("proxy") or "").strip()}
    return {
        "base_url": (p or {}).get("base_url") or os.getenv("LLM_BASE_URL") or _DEFAULT_BASE,
        "api_key": (p or {}).get("api_key") or os.getenv("LLM_API_KEY") or "",
        "model": (p or {}).get("model") or os.getenv("LLM_MODEL") or "gpt-4o-mini",
        "proxy": ((p or {}).get("proxy") or os.getenv("LLM_PROXY") or "").strip(),
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


def _new_session(cfg: dict) -> tuple["requests.Session", dict]:
    """返回 (session, proxies)。默认忽略系统代理直连；填了 proxy 才走指定代理。"""
    sess = requests.Session()
    sess.trust_env = False  # 不读取系统 HTTP_PROXY/HTTPS_PROXY，避免被错误代理 403 拦截
    proxy = (cfg.get("proxy") or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    return sess, proxies


def chat(messages: list[dict], temperature: float = 0.4, max_tokens: int = 900) -> str:
    cfg = get_config()
    if not cfg["api_key"]:
        raise LLMError("尚未配置 LLM：请在网页右上角「AI 设置」填写 base_url / api_key / model。")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    sess, proxies = _new_session(cfg)
    try:
        resp = sess.post(
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
            proxies=proxies,
        )
    except requests.RequestException as exc:
        raise LLMError(f"调用 LLM 失败（网络/地址/代理错误）：{exc}") from exc

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
    sess, proxies = _new_session(cfg)
    try:
        resp = sess.post(
            url,
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": cfg["model"], "messages": messages, "temperature": temperature,
                  "max_tokens": max_tokens, "stream": True},
            timeout=90,
            stream=True,
            proxies=proxies,
        )
    except requests.RequestException as exc:
        raise LLMError(f"调用 LLM 失败（网络/地址/代理错误）：{exc}") from exc

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


def build_news_tag_messages(batch: list[dict]) -> list[dict]:
    """批量新闻情绪打标：要求模型只输出 JSON 数组，省 token。

    batch: [{"i": 序号, "title": .., "summary": ..}]
    """
    system = (
        "你是中文财经新闻分析助手。对每条新闻判断它对相关 A 股板块/主题的情绪倾向。"
        "严格只输出一个 JSON 数组，不要任何多余文字或解释。"
        "数组每项格式：{\"i\": 序号(整数), \"sentiment\": \"利好\"|\"利空\"|\"中性\", "
        "\"score\": -100到100的整数(利好为正、利空为负、中性接近0), "
        "\"sectors\": [最多3个受影响的板块或主题中文短词]}。"
    )
    lines = [f'{n["i"]}. {n["title"]}｜{n.get("summary", "")}' for n in batch]
    user = "请逐条分析以下新闻并输出 JSON 数组：\n" + "\n".join(lines)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_json_array(text: str) -> list:
    """从模型返回里稳健提取 JSON 数组（容忍 ```json 代码块/多余文字）。"""
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("\n") + 1:] if "\n" in s else s
    lo, hi = s.find("["), s.rfind("]")
    if lo == -1 or hi == -1 or hi <= lo:
        return []
    try:
        out = json.loads(s[lo:hi + 1])
        return out if isinstance(out, list) else []
    except Exception:
        return []


def build_stock_intel_messages(name: str, code: str, tagged: list[dict],
                               net: float, pref: str = "balanced") -> list[dict]:
    """消息面（事件驱动）解读：基于已打标的个股新闻给出综合判断。"""
    pref_name = _PREF_GUIDE.get(pref, _PREF_GUIDE["balanced"])[0]
    lines = []
    for n in tagged[:10]:
        tag = n.get("sentiment", "中性")
        lines.append(f"[{tag}] {n.get('time','')} {n.get('title','')}")
    news_text = "\n".join(lines) if lines else "（暂无可用新闻）"
    system = (
        "你是严谨的 A 股『消息面/事件驱动』分析师，面向新手用通俗中文。"
        "只能基于下方已标注情绪的新闻作答，不编造；新闻可能滞后或已被price-in，"
        "务必提醒『消息≠涨跌、需与价格/资金印证』，并强调非投资建议。\n"
        f"用户偏好：{pref_name}。\n"
        "输出结构：①【消息面总体】偏多/偏空/中性 + 一句话；②【关键催化/利空】最多列 3 条；"
        "③【对趋势的潜在影响】结合可能的板块联动；④【风险/证伪点】。控制在 200-300 字。"
    )
    user = (
        f"股票：{name}（{code}）\n"
        f"近期新闻情绪净值（利好为正）：{net}\n"
        f"已标注新闻：\n{news_text}\n\n请给出消息面解读。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


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


# ---- 投资大师人设：每位大师 = 一个可单独调用的「skill」 ----
MASTER_PERSONA = {
    "value": {
        "speaker": "沃伦·巴菲特（价值/安全边际）",
        "philosophy": (
            "你信奉价值投资：只买能看懂的好生意，看重护城河、长期盈利能力与『安全边际』；"
            "用近五年估值分位判断贵贱，别人贪婪时恐惧、别人恐惧时贪婪；偏好分批买入、长期持有，"
            "不被短期波动吓走，但若估值高得离谱或基本面恶化会离场。"),
    },
    "growth": {
        "speaker": "彼得·林奇（成长/PEG）",
        "philosophy": (
            "你擅长成长股，核心是『成长与估值是否匹配』——用 PEG（市盈率÷增速）衡量，PEG<1 才有性价比；"
            "你喜欢能讲清楚的生意、稳定增长，警惕『成长跟不上估值』的高位股，成长一旦转负就果断退出。"),
    },
    "trend": {
        "speaker": "趋势交易者（利弗莫尔/德鲁肯米勒）",
        "philosophy": (
            "你顺势而为、做右侧交易：只在大方向（周/月线）向上时参与，回踩关键均线（MA20）企稳才买，"
            "绝不逆势抄底；信奉『截断亏损、让利润奔跑』，跌破趋势线/MA60 或固定比例必须止损，"
            "超买贴上轨时减仓锁利。"),
    },
    "rebound": {
        "speaker": "约翰·邓普顿（逆向/超跌）",
        "philosophy": (
            "你在『最悲观的时点』逆向买入：RSI 超卖、贴近布林下轨的恐慌错杀里找超跌反弹机会；"
            "但你纪律极严——仓位要小、只博到中轨/MA20 就走，一旦跌破近期低点立刻止损，绝不恋战。"),
    },
    "quality": {
        "speaker": "查理·芒格（质量/护城河）",
        "philosophy": (
            "你只买『伟大的生意』并在合理价格买入：看重高且稳定的 ROE、高毛利率、低负债等护城河证据，"
            "用多元思维模型判断生意质量；宁可错过也不将就平庸生意，买入后长期持有，基本面恶化才离场。"),
    },
    "aggressive_growth": {
        "speaker": "凯茜·伍德（颠覆式高成长）",
        "philosophy": (
            "你聚焦高速成长与颠覆性创新：看重营收/利润的高增长斜率与赛道空间，愿为成长容忍阶段性高估值，"
            "拿得住剧烈波动；但一旦成长熄火（增速转负/逻辑被证伪）会果断退出。"),
    },
    "risk": {
        "speaker": "纳西姆·塔勒布（尾部风险/反脆弱）",
        "philosophy": (
            "你是风险守门人：最在意『活下来、别被尾部风险击穿』。看重波动率、最大回撤与盈亏不对称性，"
            "只在『下行有限、上行可观』时才下重注；强调先定止损、按波动控制仓位、分批进出、永远留余地。"),
    },
}

# 风控型大师（不给买卖点，给仓位与风险评估）
_RISK_KEYS = {"risk"}


def build_master_messages(stock: dict, funda: dict | None, plan: dict | None,
                          master_key: str) -> list[dict]:
    """单个大师 skill：用该大师的第一人称口吻 + 规则引擎为他算好的价位，给买卖解读。"""
    persona = MASTER_PERSONA.get(master_key)
    if not persona:
        raise LLMError(f"未知的大师：{master_key}")
    master = None
    for m in (plan or {}).get("masters", []):
        if m.get("key") == master_key:
            master = m
            break

    q = stock["quote"]
    k = stock["kline"]
    latest = {
        "收盘": q["close"], "涨跌幅%": q["pct"], "日期": q["date"],
        "MA20": k["ma20"][-1], "MA60": k["ma60"][-1], "RSI12": k["rsi12"][-1],
        "布林上轨": k["boll_upper"][-1], "布林下轨": k["boll_lower"][-1],
    }
    if master:
        bz = f"买入区{master['buy_zone']}" if master.get("buy_zone") else "（当前无明确买区）"
        tp = f"，止盈≈{master['take_profit']}" if master.get("take_profit") is not None else ""
        sl = f"，止损≈{master['stop_loss']}" if master.get("stop_loss") is not None else ""
        rule_line = f"我的纪律给出：{master['action']} —— {bz}{tp}{sl}。依据：{master['reason']}"
    else:
        rule_line = "（规则引擎暂无该维度结论，请基于数据谨慎判断。）"

    if master_key in _RISK_KEYS:
        structure = (
            "输出要简洁（150-300字），固定结构：\n"
            "①【风险评估】波动率/回撤/盈亏比说明这只票危不危险；\n"
            "②【建议仓位】单票仓位上限 + 为什么；\n"
            "③【保命纪律】止损与分批进出原则；\n"
            "④【最担心的尾部风险】一句话。\n"
            "结尾用一句你风格的话，并提醒这不是投资建议。"
        )
    else:
        structure = (
            "下方『我的纪律』是规则引擎已按你的风格算好的具体价位，请采纳这些数字并用你的投资哲学解释为什么。\n"
            "输出要简洁（150-300字），固定结构：\n"
            "①【我的结论】看多/看空/观望 + 一句话理由；\n"
            "②【我会在哪买】具体价位或触发条件；\n"
            "③【我会在哪卖/离场】止盈位 + 止损位；\n"
            "④【我最担心的风险】一句话。\n"
            "结尾用一句你风格的话，并提醒这不是投资建议。"
        )
    system = (
        f"你现在以投资大师『{persona['speaker']}』的身份，用第一人称中文解读这只 A 股。\n"
        f"{persona['philosophy']}\n"
        "只能基于提供的数据，不编造数字；数据为日线非实时；不要断言一定涨跌，不说满仓/梭哈。\n"
        + structure
    )
    extra = "\n".join(x for x in (_trends_text(stock), _valuation_text(funda)) if x)
    user = (
        f"股票：{stock['name']}（{stock['code']}）\n"
        f"最新数据：{json.dumps(latest, ensure_ascii=False)}\n"
        f"{extra}\n"
        f"{rule_line}\n\n"
        f"请以『{persona['speaker']}』的身份给出你的买卖解读。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


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
        "2) 投资大师怎么看：综合【价值(巴菲特/格雷厄姆)】【质量护城河(芒格)】【成长(林奇)】"
        "【高成长颠覆(伍德)】【趋势(顺势)】【超跌反弹(邓普顿)】各派观点，并参考【风险经理(塔勒布)】的仓位建议，"
        "对一致与分歧之处都点出，数据不足就直说；\n"
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
