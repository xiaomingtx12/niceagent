"""R3 业务 subagents 配置转换。

AgentConfig.subagents 是业务 dict 格式 (AppConfig.subagents JSONB 同构):

    {
        "researcher": {
            "description": "研究员, 擅长搜索与归纳",
            "system_prompt": "你是研究员...",
            "tools": [],           # 可选, 不传继承主 agent
            "model": "openai:gpt-4o",  # 可选, 不传继承主 agent
        },
        "remote-worker": {
            "description": "远端异步任务执行器",
            "graph_id": "my-graph",    # 有 graph_id → AsyncSubAgent
            "url": "https://...",      # 可选
            "headers": {"x-api-key": "..."},  # 可选
        },
    }

转换成 deepagents `create_deep_agent(subagents=[...])` 需要的 TypedDict 列表:
- 有 `graph_id` 的条目 → AsyncSubAgent (deepagents 路由到 AsyncSubAgentMiddleware,
  agent 获得 launch/check/update/cancel/list 异步任务工具)
- 其余 → SubAgent (deepagents 路由到 SubAgentMiddleware, agent 获得 task 工具)

上下文分离 (deepagents 原生 NONE 模式):
- deepagents task 工具调 sub-agent 时排除 messages / todos / structured_response,
  只带 task description 一条 HumanMessage —— sub-agent 天然看不到父对话历史
- 其余 state 字段 (runtime_account_id 等 L1 业务身份) 自动透传
- SUMMARY / RECENT_K 模式是 R4+ 业务扩展 (把摘要拼进 description), 本模块不做

约束 (硬约束 #5): 不自己写 SubAgentMiddleware / AsyncSubAgentMiddleware;
只做业务 dict → deepagents spec 的纯函数转换。
"""
from __future__ import annotations

from typing import Any


class SubAgentSpecError(ValueError):
    """业务 subagents 配置非法 (缺必填字段)。"""


def build_subagent_specs(subagents: dict[str, dict] | None) -> list[dict[str, Any]]:
    """业务 subagents dict → deepagents spec 列表。

    Args:
        subagents: AgentConfig.subagents; key 是 sub-agent 名, value 是配置 dict.
            None / 空 dict → 返回 [] (不挂 SubAgentMiddleware / AsyncSubAgentMiddleware,
            deepagents 只保留内置 general-purpose sub-agent)

    Returns:
        deepagents SubAgent / AsyncSubAgent TypedDict 列表 (dict 形式,
        create_deep_agent 靠 "graph_id" in spec 路由)

    Raises:
        SubAgentSpecError: 配置缺必填字段
            - sync: description + system_prompt 必填
            - async (有 graph_id): description 必填
    """
    if not subagents:
        return []

    specs: list[dict[str, Any]] = []
    for name, cfg in subagents.items():
        if not isinstance(cfg, dict):
            raise SubAgentSpecError(
                f"subagent {name!r} 配置必须是 dict, 得到 {type(cfg).__name__}"
            )
        description = cfg.get("description", "")
        if not description:
            raise SubAgentSpecError(f"subagent {name!r} 缺 description (主 agent 靠它决定何时委派)")

        if "graph_id" in cfg:
            # AsyncSubAgent: 远端 Agent Protocol 服务器
            spec: dict[str, Any] = {
                "name": name,
                "description": description,
                "graph_id": cfg["graph_id"],
            }
            if cfg.get("url"):
                spec["url"] = cfg["url"]
            if cfg.get("headers"):
                spec["headers"] = cfg["headers"]
            specs.append(spec)
            continue

        # SubAgent: 声明式同步 sub-agent
        system_prompt = cfg.get("system_prompt", "")
        if not system_prompt:
            raise SubAgentSpecError(f"subagent {name!r} 缺 system_prompt (同步 sub-agent 必填)")
        spec = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
        }
        if cfg.get("tools"):
            spec["tools"] = cfg["tools"]
        if cfg.get("model"):
            spec["model"] = cfg["model"]
        specs.append(spec)

    return specs


__all__ = ["SubAgentSpecError", "build_subagent_specs"]
