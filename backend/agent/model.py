"""模型适配层：接第三方 OpenAI 兼容 API，复用 llm.get_config()（多档配置 + 每档代理）。

- DeepSeek 端点用 langchain-deepseek 的 ChatDeepSeek（ChatOpenAI 子类），它会把
  非标准的 reasoning_content（思考过程）解析到 additional_kwargs，从而能流式展示思考。
  langchain-openai 原生 ChatOpenAI 会丢弃该字段。
- 其它 OpenAI 兼容端点回退到 ChatOpenAI。
"""
from __future__ import annotations

import httpx

from .. import llm


def _is_deepseek(base_url: str, model: str) -> bool:
    return "deepseek" in (base_url or "").lower() or (model or "").lower().startswith("deepseek")


def build_model(temperature: float = 0.3, max_tokens: int = 2000):
    cfg = llm.get_config()
    if not cfg.get("api_key"):
        raise llm.LLMError("尚未配置 LLM：请在网页左下「⚙ AI」填写 base_url / api_key / model。")
    proxy = (cfg.get("proxy") or "").strip() or None
    # trust_env=False：不读系统 HTTP(S)_PROXY，避免被错误代理拦截（与 llm._new_session 一致）
    sync_client = httpx.Client(trust_env=False, proxy=proxy, timeout=120.0)
    async_client = httpx.AsyncClient(trust_env=False, proxy=proxy, timeout=120.0)
    common = dict(model=cfg["model"], api_key=cfg["api_key"],
                  temperature=temperature, max_tokens=max_tokens,
                  http_client=sync_client, http_async_client=async_client)
    if _is_deepseek(cfg["base_url"], cfg["model"]):
        from langchain_deepseek import ChatDeepSeek
        return ChatDeepSeek(api_base=cfg["base_url"], **common)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(base_url=cfg["base_url"], **common)
