"""工具层（分层管理）：把现有 agent_tools 的 11 个能力封装成 LangChain 工具。

单一事实来源仍是 backend/agent_tools.py 里的 t_* 实现；这里只做 LangChain 适配
（声明 schema / 描述 / 统一序列化为 JSON 字符串）。分层见 TIERS。
"""
from __future__ import annotations

import json

from langchain_core.tools import tool

from .. import agent_tools as A


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ---- L1 行情层（只读、低成本）----
@tool
def search_stocks(query: str) -> str:
    """按名称或代码模糊搜索股票/ETF，拿到准确的 6 位代码。query 为名称或代码关键词。"""
    return _j(A.t_search_stocks(query=query))


@tool
def get_market_overview() -> str:
    """获取大盘行情：主要指数点位涨跌 + 全市场涨跌家数。"""
    return _j(A.t_get_market_overview())


@tool
def get_sector_board(kind: str = "industry") -> str:
    """获取行业/概念板块热度榜（领涨/领跌）。kind 取 industry(行业) 或 concept(概念)。"""
    return _j(A.t_get_sector_board(kind=kind))


@tool
def get_global_news(limit: int = 12) -> str:
    """获取最新财经快讯（含已缓存的情绪标签）。limit 默认12，最多30。"""
    return _j(A.t_get_global_news(limit=limit))


@tool
def get_stock_news(code: str, limit: int = 8) -> str:
    """获取某只个股的近期相关新闻。code 为 6 位代码，limit 默认8。"""
    return _j(A.t_get_stock_news(code=code, limit=limit))


# ---- L2 分析层（计算、中成本）----
@tool
def get_stock_analysis(code: str) -> str:
    """获取个股技术面：综合判断、信号灯(趋势/MACD/RSI/布林/量能)、多周期趋势、现价涨跌。code 为 6 位代码。"""
    return _j(A.t_get_stock_analysis(code=code))


@tool
def get_fundamentals(code: str) -> str:
    """获取个股基本面：PE/PB 及历史分位与中枢、最近一期财务、主力资金流。code 为 6 位代码。"""
    return _j(A.t_get_fundamentals(code=code))


@tool
def get_master_plan(code: str) -> str:
    """获取『投资大师』操作计划：买入区/止盈/止损价位、各大师多空分与置信度、委员会总体倾向、估值锚与安全边际。code 为 6 位代码。"""
    return _j(A.t_get_master_plan(code=code))


@tool
def run_backtest(code: str, strategy: str = "composite", days: int = 250) -> str:
    """对个股运行某技术策略历史回测，返回收益/超额/胜率/最大回撤/夏普等。
    strategy 取 composite(综合)/trend(趋势)/macd(金叉)/rebound(超跌反弹)，days 默认250。"""
    return _j(A.t_run_backtest(code=code, strategy=strategy, days=days))


# ---- L3 用户态层（"我的"概念的唯一来源）----
@tool
def get_watchlist() -> str:
    """获取用户的自选股列表（代码与名称）。"""
    return _j(A.t_get_watchlist())


@tool
def get_portfolio() -> str:
    """获取用户的实际持仓：代码/数量/成本/现价/浮动盈亏%/技术判断。"""
    return _j(A.t_get_portfolio())


TIERS = {
    "L1_行情": [search_stocks, get_market_overview, get_sector_board, get_global_news, get_stock_news],
    "L2_分析": [get_stock_analysis, get_fundamentals, get_master_plan, run_backtest],
    "L3_用户态": [get_watchlist, get_portfolio],
}

TOOLS = [t for group in TIERS.values() for t in group]
