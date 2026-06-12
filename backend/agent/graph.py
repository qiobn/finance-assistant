"""编排层：LangGraph 的 ReAct 智能体图。

P1：
- 持久化 checkpointer 用 AsyncSqliteSaver（落盘 data/agent_checkpoints.db），重启不丢多轮对话。
- pre_model_hook 做 Token 预算 + 摘要（见 memory.py），控制长对话上下文。
- 模型每次按当前配置实时构建（支持用户切换 LLM 档位）。
"""
from __future__ import annotations

import asyncio

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt import create_react_agent

from .. import storage
from .memory import pre_model_hook
from .model import build_model
from .prompt import SYSTEM
from .tools import TOOLS

_DB_PATH = storage._DATA_DIR / "agent_checkpoints.db"

_saver: AsyncSqliteSaver | None = None
_saver_lock = asyncio.Lock()


async def _get_saver() -> AsyncSqliteSaver:
    """进程级单例：长连接的异步 SQLite checkpointer。"""
    global _saver
    if _saver is None:
        async with _saver_lock:
            if _saver is None:
                conn = await aiosqlite.connect(str(_DB_PATH))
                saver = AsyncSqliteSaver(conn)
                await saver.setup()
                _saver = saver
    return _saver


async def build_graph():
    """构建 ReAct 图（异步：需要拿到持久化 checkpointer）。"""
    saver = await _get_saver()
    return create_react_agent(
        build_model(), TOOLS, prompt=SYSTEM,
        checkpointer=saver, pre_model_hook=pre_model_hook,
    )
