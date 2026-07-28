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

import re
from typing import Any


class SubAgentSpecError(ValueError):
    """业务 subagents 配置非法 (缺必填字段)。"""


MAX_PLATFORM_SUBAGENTS = 8
MAX_SUBAGENT_DESCRIPTION_LENGTH = 500
MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH = 8000
_PLATFORM_SUBAGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PLATFORM_SYNC_FIELDS = frozenset({"description", "system_prompt"})


def validate_platform_subagents(
    subagents: dict[str, dict] | None,
) -> dict[str, dict[str, str]]:
    """校验并规范化 Creator API 可持久化的同步 sub-agent 配置。

    平台首期只持久化声明式同步 sub-agent。模型和工具不单独存储：
    DeepAgents 会让缺少 `model` / `tools` 的 sub-agent 继承主 Agent，
    从而继续经过 my-dify 的模型与工具权限边界。

    Args:
        subagents: AppConfig/AppConfigVersion JSONB 中的团队配置。

    Returns:
        去除描述和提示词首尾空白后的新字典。

    Raises:
        SubAgentSpecError: 配置不是受支持的同步团队结构。
    """
    if subagents is None:
        return {}
    if not isinstance(subagents, dict):
        raise SubAgentSpecError("subagents 必须是对象")
    if len(subagents) > MAX_PLATFORM_SUBAGENTS:
        raise SubAgentSpecError(
            f"subagents 数量不能超过 {MAX_PLATFORM_SUBAGENTS}"
        )

    normalized: dict[str, dict[str, str]] = {}
    for name, config in subagents.items():
        if not isinstance(name, str) or not _PLATFORM_SUBAGENT_NAME_PATTERN.fullmatch(
            name
        ):
            raise SubAgentSpecError(
                "sub-agent 名称必须以小写字母开头，且只能包含小写字母、数字、_、-，长度不超过 64"
            )
        if not isinstance(config, dict):
            raise SubAgentSpecError(f"subagent {name!r} 配置必须是对象")
        unexpected_fields = set(config) - _PLATFORM_SYNC_FIELDS
        if unexpected_fields:
            fields = ", ".join(sorted(unexpected_fields))
            raise SubAgentSpecError(
                f"subagent {name!r} 包含暂不支持的字段: {fields}"
            )

        description = config.get("description")
        system_prompt = config.get("system_prompt")
        if not isinstance(description, str) or not description.strip():
            raise SubAgentSpecError(f"subagent {name!r} 缺 description")
        if len(description) > MAX_SUBAGENT_DESCRIPTION_LENGTH:
            raise SubAgentSpecError(
                f"subagent {name!r} description 不能超过 {MAX_SUBAGENT_DESCRIPTION_LENGTH} 个字符"
            )
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise SubAgentSpecError(f"subagent {name!r} 缺 system_prompt")
        if len(system_prompt) > MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH:
            raise SubAgentSpecError(
                f"subagent {name!r} system_prompt 不能超过 {MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH} 个字符"
            )
        normalized[name] = {
            "description": description.strip(),
            "system_prompt": system_prompt.strip(),
        }
    return normalized


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


__all__ = [
    "MAX_PLATFORM_SUBAGENTS",
    "SubAgentSpecError",
    "build_subagent_specs",
    "validate_platform_subagents",
]
