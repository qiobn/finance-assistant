"""模型适配层：用 langchain-openai 的 ChatOpenAI 接第三方 OpenAI 兼容 API。

复用项目现有的 llm.get_config()（多档配置 + 每档代理），不绑定任何托管服务。
"""
from __future__ import annotations

import httpx
from langchain_openai import ChatOpenAI

from .. import llm


def build_model(temperature: float = 0.3, max_tokens: int = 2000) -> ChatOpenAI:
    cfg = llm.get_config()
    if not cfg.get("api_key"):
        raise llm.LLMError("尚未配置 LLM：请在网页左下「⚙ AI」填写 base_url / api_key / model。")
    proxy = (cfg.get("proxy") or "").strip() or None
    # trust_env=False：不读系统 HTTP(S)_PROXY，避免被错误代理拦截（与 llm._new_session 一致）
    sync_client = httpx.Client(trust_env=False, proxy=proxy, timeout=120.0)
    async_client = httpx.AsyncClient(trust_env=False, proxy=proxy, timeout=120.0)
    return ChatOpenAI(
        base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"],
        temperature=temperature, max_tokens=max_tokens,
        http_client=sync_client, http_async_client=async_client,
    )
