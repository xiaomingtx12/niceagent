"""R2 业务 SummarizationMiddleware。

复用 deepagents `_DeepAgentsSummarizationMiddleware` (硬约束 #5: 不重写
_should_summarize / _partition_messages / offload 逻辑)。本模块只负责:
1. 把 `AgentConfig.max_message_count` 透传成 deepagents 的 trigger 参数
2. 提供一个 thin 子类把 `.name` 改成 "MyDifySummarizationMiddleware",
   避免与 deepagents `create_deep_agent` base stack 自带的 default
   SummarizationMiddleware (trigger=None) 撞名触发
   `langchain.agents.factory.create_agent` 的
   `Please remove duplicate middleware instances.` AssertionError。

业务约定:
- trigger: `("messages", AgentConfig.max_message_count)` (默认 20)
- keep: `("messages", max(10, max_message_count // 2))` —— 比 trigger 小,
  保证压缩总会发生
- model: 用主 agent 的 LLM (不开独立 summary LLM, 节省成本)
- backend: 复用 R1 MyDifyWorkspaceBackend —— 把超长 tool_call 内容 offload 到
  workspace_files; 不传时用 deepagents StateBackend 占位
- trigger=None (R2 之前 max_message_count=0 时) → 永不触发, 等价于关闭压缩

state schema 字段 (deepagents 中间件自动管理):
- `_summarization_event: dict | None` —— 最近一次摘要事件, 含 cutoff_index
  / summary_message / file_path; BaseAgent 可读这个字段给前端展示
  "已压缩 N 条历史" 提示

测试策略 (硬约束 #5):
- 纯函数 build_summarization_trigger / build_summarization_keep 覆盖
  trigger / keep 各种边界, 无 LLM / DB 依赖
- build_summarization_middleware 返回 deepagents 中间件实例, 验证:
  - .name 是 "MyDifySummarizationMiddleware" (避免与 base stack 同名)
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
    """业务 SummarizationMiddleware, 仅重写 `.name` 避免与 deepagents base stack 撞名。

    deepagents `create_deep_agent` 默认会在 base stack 挂一个 trigger=None 的
    `_DeepAgentsSummarizationMiddleware` (其 `.name` property 返回 alias
    "SummarizationMiddleware")。如果我们在 `middleware=[...]` 里再 append 一个
    `SummarizationMiddleware(...)` 实例, 两者 `.name` 相同, 会触发
    `langchain.agents.factory.create_agent` line 1080:
        if len({m.name for m in middleware}) != len(middleware):
            raise AssertionError("Please remove duplicate middleware instances.")

    重写 `.name` 为 "MyDifySummarizationMiddleware" 即可与 base stack 区分开.
    其余逻辑 (wrap_model_call / _should_summarize / _partition_messages / 后端
    offload) 完全沿用 deepagents 实现 —— 硬约束 #5: 不重写 deepagents 已有逻辑.
    """

    @property
    def name(self) -> str:
        # _DeepAgentsSummarizationMiddleware.name 用了 type check (`type(self) is ...`);
        # 我们的子类会 fall through 到 type(self).__name__ = "MyDifySummarizationMiddleware".
        # 但显式返回更稳, 避免以后 deepagents 改 property 行为.
        return "MyDifySummarizationMiddleware"


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
        - None: max_message_count <= 0 (关闭压缩)
        - MyDifySummarizationMiddleware 实例 (`.name` 区别于 deepagents
          base stack default, 避免 create_agent 撞名校验失败)
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
