"""运行层：把 LangGraph 的事件流转成前端的 NDJSON 契约（token 级流式）。

事件：
  {"type":"tool",      "name":..., "label":..., "args":{...}}  # 正在调用工具
  {"type":"reasoning", "text":"..."}                            # 思考模型的推理过程增量
  {"type":"delta",     "text":"..."}                            # 最终答案 token 增量
  {"type":"done"}                                               # 本轮结束
  {"type":"error",     "text":"..."}                            # 出错
"""
from __future__ import annotations

import json

from .. import agent_tools, llm
from .graph import build_graph

RECURSION_LIMIT = 14  # ≈6 轮工具往返，触顶即优雅收尾（兜底）


def _evt(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _chunk_text(chunk) -> str:
    c = getattr(chunk, "content", "") if chunk is not None else ""
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
    got_text = False
    try:
        async for ev in graph.astream_events(inputs, config=config, version="v2"):
            etype = ev.get("event")
            if etype == "on_tool_start":
                name = ev.get("name", "")
                args = (ev.get("data") or {}).get("input") or {}
                yield _evt({"type": "tool", "name": name,
                            "label": agent_tools.TOOL_LABELS.get(name, name), "args": args})
            elif etype == "on_chat_model_stream":
                # 只取主 agent 节点的输出；摘要节点(pre_model_hook)的 LLM 调用不外泄
                if (ev.get("metadata") or {}).get("langgraph_node") != "agent":
                    continue
                chunk = (ev.get("data") or {}).get("chunk")
                if chunk is None:
                    continue
                rc = (getattr(chunk, "additional_kwargs", {}) or {}).get("reasoning_content")
                if rc:  # 思考模型（如 deepseek-reasoner）的推理增量
                    yield _evt({"type": "reasoning", "text": rc})
                txt = _chunk_text(chunk)
                if txt:
                    got_text = True
                    yield _evt({"type": "delta", "text": txt})
        if not got_text:
            yield _evt({"type": "delta", "text": "（模型未返回内容，请重试）"})
        yield _evt({"type": "done"})
    except Exception as exc:  # noqa: BLE001 工具/模型异常统一兜底，不让流崩
        msg = str(exc)
        if "recursion" in msg.lower():
            yield _evt({"type": "delta", "text": "分析步骤较多未能收敛，请把问题拆细一点再问。"})
            yield _evt({"type": "done"})
        else:
            yield _evt({"type": "error", "text": f"智能体出错：{msg[:160]}"})
