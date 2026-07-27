"""R0 业务身份上下文中间件（替代 master 老 `<长期记忆>` 注入）。

master 老做法：
- AgentConfig.enable_long_term_memory → AGENT_SYSTEM_PROMPT_TEMPLATE 里的
  <长期记忆>{long_term_memory}</长期记忆> 占位 → before_agent 节点异步拉
  summary 写入 AgentState.long_term_memory

R0 新做法（按 [deepagents-v2-plan-context-design](memory)）：
- L1 业务身份不走文件系统（不是 /AGENTS.md），走**业务模型驱动**——
  AppConfig + Account + App + AppConfigVersion + Conversation 直接渲染成
  system_prompt 的"业务身份段"
- 这个中间件在 `before_agent` 把当前 invoke 的运行时上下文（app_id、app 名称、
  preset_prompt、当前时间、当前账号）写入 state，**不写入** system_prompt
  —— system_prompt 已经在 factory 里渲染好了
- L2 Skills 元数据 / L3 动态上下文 / L4 对话历史由后续阶段（明文 skills +
  memory_facts + filesystem）单独接入，本中间件只负责 L1

R4 扩展（按 [deepagents-v2-plan-user-memory-visibility](memory)）：
- L3 动态上下文召回：可选接 memory_fact_service → recall_top_k → 写入
  state['runtime_recalled_facts']。下游 wrap_model_call hook 把这些 fact
  拼到 system_prompt 末尾（plan §1.5）。
- 不接时（默认）不召任何 fact，state 里也没这个字段，行为与 R0 一致。

约束：
- 不读文件系统（硬约束：my-dify 是业务平台不引入文件系统 SKILL.md/AGENTS.md）
- 不调 LLM（before_agent 阶段还没进 graph）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from langchain.agents.middleware.types import AgentMiddleware


class ProjectContextMiddleware(AgentMiddleware):
    """L1 业务身份运行时上下文注入 (+ 可选 L3 fact 召回).

    state 字段（仅供 graph 内节点使用）：
    - `runtime_app_id: str` —— 当前 App ID（用于 audit log / review_config）
    - `runtime_account_id: str` —— 当前账号（用于多租户隔离）
    - `runtime_conversation_id: str` —— 当前对话
    - `runtime_invoked_at: str` —— ISO8601 时间戳
    - `runtime_recalled_facts: list[dict]` (可选, R4+) —— 召回到的 fact 列表
        （MemoryFact.to_dict() 形式）；下游 wrap_model_call hook 拼到 system prompt

    R4 召回语义：
    - memory_fact_service 提供时按 (account_id, app_id, end_user_id) recall top-K
    - end_user_id=None → 只召 account 级（creator 调试 / 早期无 end-user 鉴权）
    - 不提供 memory_fact_service → 不召回, state 无 runtime_recalled_facts 字段
    """

    state_schema = Any

    def __init__(
        self,
        *,
        account_id: str,
        app_id: str,
        conversation_id: str,
        end_user_id: Optional[str] = None,
        memory_fact_service: Any = None,
        recall_k: int = 5,
    ) -> None:
        super().__init__()
        self._account_id = account_id
        self._app_id = app_id
        self._conversation_id = conversation_id
        self._end_user_id = end_user_id
        self._memory_fact_service = memory_fact_service
        self._recall_k = recall_k

    def before_agent(self, state: dict, runtime: Any) -> dict | None:
        """注入运行上下文到 state, 不修改 messages / system_prompt.

        R4: 若 memory_fact_service 提供, 召回 top-K fact 写到
        state['runtime_recalled_facts'] (to_dict 形式). 下游中间件
        (R5+ wrap_model_call) 会把该字段拼到 system prompt 末尾.

        注意：master 老代码会在 <长期记忆>{long_term_memory}</长期记忆> 段注入
        摘要，R0 不再做这件事——摘要由 R2 的 `SummarizationMiddleware` 接管，
        通过 `messages` 截断 + `/conversation_history/{cnv_id}.md` filesystem
        累积完成，**不写回 system_prompt**。
        """
        out: dict[str, Any] = {
            "runtime_account_id": self._account_id,
            "runtime_app_id": self._app_id,
            "runtime_conversation_id": self._conversation_id,
            "runtime_invoked_at": datetime.now(timezone.utc).isoformat(),
        }
        # R4: 召回 memory_fact top-K
        if self._memory_fact_service is not None:
            try:
                facts = self._memory_fact_service.recall_top_k(
                    account_id=self._account_id,
                    app_id=self._app_id,
                    end_user_id=self._end_user_id,
                    k=self._recall_k,
                )
                out["runtime_recalled_facts"] = [f.to_dict() for f in facts]
            except Exception:
                # 召回失败不阻塞 agent invoke; L3 上下文降级到空
                out["runtime_recalled_facts"] = []
        return out


__all__ = ["ProjectContextMiddleware"]