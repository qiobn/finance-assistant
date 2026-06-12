"""运行层：把 LangGraph 的事件流转成前端的 NDJSON 契约（保持与手写版一致）。

事件：
  {"type":"tool",   "name":..., "label":..., "args":{...}}
  {"type":"answer", "text":"..."}
  {"type":"error",  "text":"..."}
"""
from __future__ import annotations

import json

from .. import agent_tools, llm
from .graph import build_graph

RECURSION_LIMIT = 14  # ≈6 轮工具往返，触顶即优雅收尾（兜底）


def _evt(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _content_text(msg) -> str:
    c = getattr(msg, "content", "") if msg is not None else ""
    if isinstance(c, list):  # 某些模型返回分块内容
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return c or ""


async def run_stream(thread_id: str, question: str):
    try:
        graph = await build_graph()
    except llm.LLMError as exc:
        yield _evt({"type": "error", "text": str(exc)})
        return

    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    inputs = {"messages": [{"role": "user", "content": question}]}
    try:
        async for ev in graph.astream_events(inputs, config=config, version="v2"):
            if ev.get("event") == "on_tool_start":
                name = ev.get("name", "")
                args = (ev.get("data") or {}).get("input") or {}
                yield _evt({"type": "tool", "name": name,
                            "label": agent_tools.TOOL_LABELS.get(name, name), "args": args})
        # 工具流结束后，从图状态取最终一条 assistant 消息作为答案（P0 一次性返回）
        state = await graph.aget_state(config)
        msgs = (state.values or {}).get("messages", []) if state else []
        text = _content_text(msgs[-1]).strip() if msgs else ""
        yield _evt({"type": "answer", "text": text or "（模型未返回内容，请重试）"})
    except Exception as exc:  # noqa: BLE001 工具/模型异常统一兜底，不让流崩
        msg = str(exc)
        if "recursion" in msg.lower():
            yield _evt({"type": "answer", "text": "分析步骤较多未能收敛，请把问题拆细一点再问。"})
        else:
            yield _evt({"type": "error", "text": f"智能体出错：{msg[:160]}"})
