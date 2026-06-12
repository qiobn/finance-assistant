"""投研智能体：工具调用循环（function calling）+ 多轮对话。

run_stream() 是一个生成器，逐步产出 NDJSON 事件（每行一个 JSON）：
  {"type":"tool",   "name":..., "label":..., "args":{...}}   # 正在调用某工具
  {"type":"answer", "text":"..."}                              # 最终回答（完整）
  {"type":"error",  "text":"..."}                              # 出错

会话历史由调用方维护：本函数会把 assistant / tool 消息**就地追加**到传入的
messages 列表，迭代结束后调用方即可持久化完整轨迹。
"""
from __future__ import annotations

import json

from . import agent_tools, llm

MAX_STEPS = 6  # 工具调用最多轮数，防止死循环

SYSTEM = (
    "你是一名严谨的 A 股投研助理，面向不太懂股票的普通用户，用通俗中文多轮对话。"
    "你可以调用工具获取真实数据（行情、技术面、基本面、投资大师计划、历史回测、"
    "大盘、板块、新闻、自选股、持仓）。\n"
    "原则：\n"
    "1) 凡涉及具体个股/大盘/持仓的数字与判断，必须先调用相应工具获取真实数据，不得编造数字。\n"
    "2) 严禁凭记忆猜代码：任何个股代码都要先用 search_stocks 核实，再用核实到的代码调其它工具。\n"
    "3) 『自选股/持仓/我的…』这类指向用户的概念，只能来自 get_watchlist / get_portfolio 的真实返回；"
    "绝不能把你自己挑选或联想出的股票称作用户的自选股或持仓。\n"
    "4) 当用户问某个主题/板块（如黄金、AI、半导体）而非指名个股时：可用 get_sector_board 看板块、"
    "或用 search_stocks 找相关个股；回答时明确说明这是『我为你找的相关个股』，不要假装是用户已持有或已自选的。\n"
    "5) 一个问题可按需调用多个工具（如分析持仓要先 get_portfolio 再逐只 get_master_plan）。\n"
    "6) 回答要点化、讲人话、必要时一句话解释术语；指出信号矛盾处。\n"
    "7) 始终客观中立、提示风险，强调这不是投资建议、技术指标有滞后性，不要说『一定涨/一定跌/满仓』。\n"
    "8) 数据不足或工具报错就如实说明，不要硬编。"
)


def build_initial(history: list[dict], question: str) -> list[dict]:
    """组装本轮要发给模型的完整 messages（system + 历史 + 新问题）。"""
    msgs = [{"role": "system", "content": SYSTEM}]
    msgs.extend(history or [])
    msgs.append({"role": "user", "content": question})
    return msgs


def _evt(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def run_stream(messages: list[dict]):
    """运行工具调用循环；就地把 assistant/tool 消息追加进 messages。"""
    try:
        for step in range(MAX_STEPS):
            use_tools = step < MAX_STEPS - 1  # 最后一轮强制收尾、不再给工具
            msg = llm.chat_raw(messages, tools=agent_tools.TOOLS if use_tools else None)
            tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None

            if tool_calls:
                # 先把带 tool_calls 的 assistant 消息入历史，再补每个工具结果
                messages.append({"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn = (tc.get("function") or {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    label = agent_tools.TOOL_LABELS.get(name, name)
                    yield _evt({"type": "tool", "name": name, "label": label, "args": args})
                    result = agent_tools.execute(name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                     "name": name, "content": json.dumps(result, ensure_ascii=False)})
                continue

            content = (msg.get("content") or "").strip() if isinstance(msg, dict) else ""
            messages.append({"role": "assistant", "content": content})
            yield _evt({"type": "answer", "text": content or "（模型未返回内容，请重试）"})
            return
        # 兜底：达到步数上限仍未收尾
        yield _evt({"type": "answer", "text": "分析步骤较多未能收敛，请把问题拆细一点再问。"})
    except llm.LLMError as exc:
        yield _evt({"type": "error", "text": str(exc)})
    except Exception as exc:  # noqa: BLE001
        yield _evt({"type": "error", "text": f"智能体出错：{str(exc)[:160]}"})
