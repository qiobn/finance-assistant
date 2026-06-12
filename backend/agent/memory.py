"""上下文工程：Token 预算 + 摘要节点（create_react_agent 的 pre_model_hook）。

策略（只影响发给 LLM 的输入，不改写 checkpointer 里持久化的完整历史）：
- 历史字符数在预算内：原样透传。
- 超预算：保留最近一段原始消息（以 human 起始，避免 tool 消息脱离其 assistant），
  把更早的消息用一次 LLM 调用压成中文摘要，作为一条 system 消息拼在前面。

用字符数估算预算（中文友好，免 tiktoken/分词依赖；约 1 token ≈ 1.5 个中文字符）。
"""
from __future__ import annotations

from langchain_core.messages import SystemMessage, trim_messages

from .model import build_model

TRIGGER_CHARS = 9000   # 历史超过此规模才启用裁剪/摘要
RECENT_CHARS = 5000    # 裁剪后保留的最近原始消息规模


def _msg_chars(m) -> int:
    c = getattr(m, "content", "") or ""
    if isinstance(c, list):
        c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    n = len(c)
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        n += sum(len(str(tc)) for tc in tcs)
    return n


def _total_chars(msgs) -> int:
    return sum(_msg_chars(m) for m in msgs)


def _flatten(m) -> str:
    role = {"human": "用户", "ai": "助手", "tool": "工具结果", "system": "系统"}.get(
        getattr(m, "type", ""), getattr(m, "type", ""))
    c = getattr(m, "content", "") or ""
    if isinstance(c, list):
        c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        c = (c + " " + "；".join(f"调用{t.get('name')}({t.get('args')})" for t in tcs)).strip()
    return f"{role}：{c[:600]}"


def _summarize(older) -> str:
    text = "\n".join(_flatten(m) for m in older if _msg_chars(m) > 0)
    model = build_model(temperature=0.2, max_tokens=400)
    resp = model.invoke([
        SystemMessage("把下面这段投研对话压成要点摘要，保留：用户关心的股票/主题、已给出的结论与关键数字、"
                      "尚未解决的问题。用中文，250 字以内，分条。"),
        {"role": "user", "content": text[:8000]},
    ])
    c = resp.content
    return c if isinstance(c, str) else str(c)


def pre_model_hook(state):
    """create_react_agent 的前置钩子：返回 llm_input_messages 控制发给 LLM 的上下文。"""
    msgs = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
    try:
        if _total_chars(msgs) <= TRIGGER_CHARS:
            return {"llm_input_messages": msgs}
        recent = trim_messages(
            msgs, max_tokens=RECENT_CHARS, token_counter=_total_chars,
            strategy="last", start_on="human", include_system=False, allow_partial=False,
        )
        older = msgs[: len(msgs) - len(recent)] if recent else msgs[:-4]
        if not older:
            return {"llm_input_messages": msgs}
        if not recent:
            recent = msgs[-4:]
        summary = _summarize(older)
        return {"llm_input_messages":
                [SystemMessage(content="【早前对话摘要（节省上下文）】\n" + summary)] + list(recent)}
    except Exception:  # noqa: BLE001 兜底：任何异常都退回完整历史，绝不让对话因裁剪失败而中断
        return {"llm_input_messages": msgs}
