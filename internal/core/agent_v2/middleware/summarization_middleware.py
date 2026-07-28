"""业务化配置 DeepAgents 原生 SummarizationMiddleware。

本模块不重写摘要算法，只负责把 `AgentConfig.max_message_count` 转成
DeepAgents 的 ``trigger`` / ``keep`` 参数。自定义实例保留框架原生中间件名
``SummarizationMiddleware``，这样 ``create_deep_agent`` 会通过同名替换机制
替换默认实例，而不是在 middleware stack 里追加第二个摘要器。

业务约定:
- trigger: `("messages", AgentConfig.max_message_count)` (默认 20)
- keep: `("messages", max(10, max_message_count // 2))` —— 比 trigger 小,
  保证压缩总会发生
- model: 用主 agent 的 LLM (不开独立 summary LLM, 节省成本)
- backend: 复用 R1 MyDifyWorkspaceBackend —— 把超长 tool_call 内容 offload 到
  workspace_files; 不传时用 deepagents StateBackend 占位
- max_message_count <= 0 时不传自定义实例，继续使用 DeepAgents 默认的
  token-window 摘要策略

state schema 字段 (deepagents 中间件自动管理):
- `_summarization_event: dict | None` —— 最近一次摘要事件, 含 cutoff_index
  / summary_message / file_path; BaseAgent 可读这个字段给前端展示
  "已压缩 N 条历史" 提示

测试策略 (硬约束 #5):
- 纯函数 build_summarization_trigger / build_summarization_keep 覆盖
  trigger / keep 各种边界, 无 LLM / DB 依赖
- build_summarization_middleware 返回 deepagents 中间件实例, 验证:
  - .name 是 "SummarizationMiddleware" (原位替换 base stack 默认实例)
  - trigger / keep 透传正确
  - backend 透传正确
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

# 私有 alias 引用; deepagents public `SummarizationMiddleware` = `SummarizationMiddleware`
# (SummarizationMiddleware.py:1647 `SummarizationMiddleware = _DeepAgentsSummarizationMiddleware`)
# 但 import `_DeepAgentsSummarizationMiddleware` 直接走 private 名, 避免和公共名混淆
from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware


class MyDifySummarizationMiddleware(_DeepAgentsSummarizationMiddleware):
    """带业务阈值的原生摘要器替换项。

    DeepAgents 0.6.12 的 ``_apply_custom_middleware`` 会按 ``.name`` 原位替换
    core middleware。这里必须返回框架原生名称，才能保留正确顺序且只存在一个
   摘要器。
    """

    @property
    def name(self) -> str:
        return "SummarizationMiddleware"


# 业务对外名; 单测和 factory 都引用这个
SummarizationMiddleware = MyDifySummarizationMiddleware


def build_summarization_trigger(
    max_message_count: int,
) -> Optional[Tuple[str, int]]:
    """把 AgentConfig.max_message_count 翻译成 deepagents trigger tuple。

    边界:
    - max_message_count <= 0 → None (关闭压缩)
    - max_message_count == 1 → ("messages", 1) (任何非空 messages 都触发,
      保留 1 条, 业务上几乎不会用, 仅作完整契约)
    - max_message_count > 1 → ("messages", max_message_count)
    """
    if max_message_count <= 0:
        return None
    return ("messages", int(max_message_count))


def build_summarization_keep(
    max_message_count: int,
) -> Tuple[str, int]:
    """根据 trigger 反算 keep。

    keep 必须 < trigger 否则永远触发 (deadlock 风险)。
    默认保留一半, 至少 1 条。
    """
    if max_message_count <= 1:
        return ("messages", 1)
    return ("messages", max(1, max_message_count // 2))


def build_summarization_middleware(
    *,
    llm: Any,
    max_message_count: int,
    backend: Any = None,
) -> Optional[MyDifySummarizationMiddleware]:
    """构造业务 SummarizationMiddleware 实例。

    Args:
        llm: 主 agent LLM (BaseChatModel); 用于生成 summary
        max_message_count: AgentConfig.max_message_count (默认 20)
        backend: 可选 BACKEND_TYPES; R1 阶段用 MyDifyWorkspaceBackend
                 把超长 tool_call 写进 workspace_files. 不传时默认
                 用 deepagents StateBackend (in-process dict, 不持久化).
                 这是 deepagents SummarizationMiddleware backend 必填
                 参数的兜底.

    Returns:
        - None: max_message_count <= 0，使用 DeepAgents 默认摘要策略
        - MyDifySummarizationMiddleware 实例；由 DeepAgents 按同名替换默认实例
    """
    trigger = build_summarization_trigger(max_message_count)
    if trigger is None:
        return None
    keep = build_summarization_keep(max_message_count)
    if backend is None:
        # deepagents backend 必填; 不传则用 StateBackend 占位 (无 offload 效果
        # 但满足构造约束, 业务 R1 之前可用)
        from deepagents.backends import StateBackend
        backend = StateBackend()
    return MyDifySummarizationMiddleware(
        model=llm,
        backend=backend,
        trigger=trigger,
        keep=keep,
    )


__all__ = [
    "MyDifySummarizationMiddleware",
    "SummarizationMiddleware",
    "build_summarization_keep",
    "build_summarization_middleware",
    "build_summarization_trigger",
]
