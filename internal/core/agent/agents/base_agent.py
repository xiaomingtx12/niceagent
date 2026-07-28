import uuid
from threading import Thread
from typing import Optional, Any, Iterator

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.language_models import BaseChatModel
from langchain_core.load import Serializable
from langchain_core.messages import HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import ConfigDict, PrivateAttr

from internal.core.agent.entities.agent_entity import AgentConfig, AgentState
from internal.core.agent_v2.factory import build_deep_agent, build_thread_config
from internal.core.agent_v2.context import build_app_system_prompt
from internal.core.agent_v2.middleware.event_translation_middleware import (
    bind_event_translation,
)
from internal.exception import FailException
from .agent_queue_manager import AgentQueueManager
from ..entities.queue_entity import AgentResult, AgentThought, QueueEvent


class BaseAgent(Serializable, Runnable):
    """deepagents 智能体包装层。

    R0 改造后，BaseAgent 不再是抽象类（master 老版本有 FunctionCallAgent /
    ReACTAgent 两种子类）——deepagents `create_deep_agent` 内部统一用工具
    调用节点处理 function call 和 ReACT 两种推理模式（见 deepagents README
    0.6.x），由 model 自动选择，**不再需要在 graph 层分支**。

    保留责任：
    1. 调 `build_deep_agent` 组装 CompiledStateGraph
    2. 保留 master `AgentQueueManager` 异步线程协议（chat / debug_chat
       SSE 端点依赖 `yield from self._agent_queue_manager.listen(task_id)`，
       R0 阶段不动）
    3. 保留 invoke/stream 协议（输入 AgentState → 输出 AgentResult 流）

    约束：硬约束 #5：复用 deepagents / langchain 原生机制（base_agent.py
    本质是 deepagent CompiledStateGraph 的薄包装 + 业务 SSE 兼容层）。
    """

    llm: BaseChatModel
    agent_config: AgentConfig
    _agent: CompiledStateGraph = PrivateAttr(None)
    _agent_queue_manager: AgentQueueManager = PrivateAttr(None)
    _checkpointer: BaseCheckpointSaver | bool | None = PrivateAttr(None)
    _workspace_backend: BackendProtocol | None = PrivateAttr(None)
    _skill_sources: list[str] = PrivateAttr(default_factory=list)
    _memory_sources: list[str] = PrivateAttr(default_factory=list)
    _subagent_skill_sources: dict[str, list[str]] = PrivateAttr(
        default_factory=dict
    )
    _permissions: list[FilesystemPermission] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(
        # 字段允许接收任意类型，且不需要校验器
        arbitrary_types_allowed=True
    )

    name: str = "base_agent"

    def __init__(
        self,
        llm: BaseChatModel,
        agent_config: AgentConfig,
        checkpointer: BaseCheckpointSaver | bool | None = None,
        workspace_backend: BackendProtocol | None = None,
        permissions: list[FilesystemPermission] | None = None,
        skill_sources: list[str] | None = None,
        memory_sources: list[str] | None = None,
        subagent_skill_sources: dict[str, list[str]] | None = None,
        *args,
        **kwargs,
    ):
        """构造函数，调用 build_deep_agent 构造 CompiledStateGraph。"""
        super().__init__(*args, llm=llm, agent_config=agent_config, **kwargs)
        self._checkpointer = checkpointer
        self._workspace_backend = workspace_backend
        self._permissions = list(permissions or [])
        self._skill_sources = list(skill_sources or [])
        self._memory_sources = list(memory_sources or [])
        self._subagent_skill_sources = dict(subagent_skill_sources or {})
        self._agent = self._build_agent()
        self._agent_queue_manager = AgentQueueManager(
            user_id=agent_config.user_id,
            invoke_from=agent_config.invoke_from,
        )

    def _build_agent(self) -> CompiledStateGraph:
        """构造 deepagent CompiledStateGraph。

        取 agent_config 里渲染好的 system_prompt + tools + review_config，
        加上 invoke 时填到 thread_config 的 account_id/app_id/conversation_id
        (R1 起这三个 ID 是 AgentConfig 显式字段, factory 直接读; 之前 master
        老实现靠 getattr hack)。

        Checkpointer 默认关闭；同步多 Agent 委托不依赖 saver。需要跨请求恢复时，
        调用方应显式注入一个生命周期受控的 LangGraph checkpointer。
        """
        return build_deep_agent(
            llm=self.llm,
            tools=self.agent_config.tools,
            system_prompt=build_app_system_prompt(
                app_name=self.agent_config.app_name,
                app_description=self.agent_config.app_description,
                preset_prompt=self.agent_config.preset_prompt,
                opening_statement=self.agent_config.opening_statement,
            ),
            review_config=self.agent_config.review_config,
            account_id=str(self.agent_config.account_id or ""),
            app_id=str(self.agent_config.app_id or ""),
            conversation_id=str(self.agent_config.conversation_id or ""),
            checkpointer=self._checkpointer,
            workspace_backend=self._workspace_backend,
            permissions=self._permissions,
            skill_sources=self._skill_sources,
            memory_sources=self._memory_sources,
            subagent_skill_sources=self._subagent_skill_sources,
            end_user_id=(
                str(self.agent_config.end_user_id)
                if self.agent_config.end_user_id
                else None
            ),
            workflow_variables=self.agent_config.workflow_variables,
            subagents=self.agent_config.subagents,  # R3 sub-agent 配置
        )

    def invoke(
        self, input: AgentState, config: Optional[RunnableConfig] = None
    ) -> AgentResult:
        """块内容响应，一次性生成完整内容后返回"""
        # 1.调用stream方法获取流式事件输出数据
        if not input.get("messages"):
            raise FailException("智能体输入消息不能为空")
        current_message = next(
            (
                message
                for message in reversed(input["messages"])
                if isinstance(message, HumanMessage)
            ),
            input["messages"][-1],
        )
        content = current_message.content
        query = ""
        image_urls = []
        if isinstance(content, str):
            query = content
        elif isinstance(content, list):
            query = content[0]["text"]
            image_urls = [
                chunk["image_url"]["url"]
                for chunk in content
                if chunk.get("type") == "image_url"
            ]
        agent_result = AgentResult(query=query, image_urls=image_urls)
        agent_thoughts = {}
        for agent_thought in self.stream(input, config):
            # 2.提取事件id并转换成字符串
            event_id = str(agent_thought.id)

            # 3.除了ping事件，其他事件全部记录
            if agent_thought.event != QueueEvent.PING:
                # 4.单独处理agent_message事件，因为该事件为数据叠加
                if agent_thought.event == QueueEvent.AGENT_MESSAGE:
                    # 5.检测是否已存储了事件
                    if event_id not in agent_thoughts:
                        # 6.初始化智能体消息事件
                        agent_thoughts[event_id] = agent_thought
                    else:
                        # 7.叠加智能体消息事件
                        agent_thoughts[event_id] = agent_thoughts[event_id].model_copy(
                            update={
                                "thought": agent_thoughts[event_id].thought
                                + agent_thought.thought,
                                "answer": agent_thoughts[event_id].answer
                                + agent_thought.answer,
                                "latency": agent_thought.latency,
                            }
                        )
                    # 8.更新智能体消息答案
                    agent_result.answer += agent_thought.answer
                else:
                    # 9.处理其他类型的智能体事件，类型均为覆盖
                    agent_thoughts[event_id] = agent_thought

                    # 10.单独判断是否为异常消息类型，如果是则修改状态并记录错误
                    if agent_thought.event in [
                        QueueEvent.STOP,
                        QueueEvent.TIMEOUT,
                        QueueEvent.ERROR,
                    ]:
                        agent_result.status = agent_thought.event
                        agent_result.error = (
                            agent_thought.observation
                            if agent_thought.event == QueueEvent.ERROR
                            else ""
                        )

        # 11.将推理字典转换成列表并存储
        agent_result.agent_thoughts = [
            agent_thought for agent_thought in agent_thoughts.values()
        ]

        # 12.完善message
        agent_result.message = next(
            (
                agent_thought.message
                for agent_thought in agent_thoughts.values()
                if agent_thought.event == QueueEvent.AGENT_MESSAGE
            ),
            [],
        )

        # 13.更新总耗时
        agent_result.latency = sum(
            [agent_thought.latency for agent_thought in agent_thoughts.values()]
        )

        return agent_result

    def stream(
        self,
        input: AgentState,
        config: Optional[RunnableConfig] = None,
        **kwargs: Optional[Any],
    ) -> Iterator[AgentThought]:
        """流式输出，并保持现有 SSE queue/心跳/停止协议。"""
        # 1.检测 agent 是否已构建
        if not self._agent:
            raise FailException("智能体未成功构建，请核实后尝试")

        # 2.兼容旧调用的 history 字段，并避免修改调用方传入的字典。
        stream_input = dict(input)
        history = list(stream_input.pop("history", []) or [])
        current_messages = list(stream_input.get("messages", []))
        stream_input["task_id"] = stream_input.get("task_id", uuid.uuid4())
        stream_input["iteration_count"] = stream_input.get("iteration_count", 0)
        effective_config = self._merge_thread_config(config)
        has_checkpoint = False
        if self._checkpointer not in (None, False) and effective_config is not None:
            try:
                snapshot = self._agent.get_state(effective_config)
                has_checkpoint = bool(snapshot.values)
            except Exception:
                # Saver availability errors must still surface during graph
                # execution; this check only decides whether DB history is a
                # first-run bootstrap.
                has_checkpoint = False
        stream_input["messages"] = (
            current_messages if has_checkpoint else history + current_messages
        )

        # 3.创建子线程执行 deepagent stream
        # 后台线程跑图，当前线程继续由 queue manager 负责心跳与停止标记。
        thread = Thread(
            target=self._run_stream_in_thread,
            args=(stream_input, effective_config),
            kwargs=kwargs,
        )
        thread.daemon = True
        thread.start()

        # 4.调用队列管理器监听数据并返回迭代器
        yield from self._agent_queue_manager.listen(stream_input["task_id"])

    def _run_stream_in_thread(
        self,
        input: AgentState,
        config: Optional[RunnableConfig],
        **kwargs: Any,
    ) -> None:
        """执行 graph；所有业务事件均由 EventTranslationMiddleware 发布。"""
        graph_input = {
            key: value
            for key, value in input.items()
            if key not in {"task_id", "iteration_count"}
        }
        with bind_event_translation(
            task_id=input["task_id"],
            publisher=self._agent_queue_manager.publish,
        ) as event_session:
            try:
                # 消费 updates 仅用于驱动 graph，BaseAgent 不再解析框架内部 chunk。
                for _ in self._agent.stream(
                    graph_input,
                    config=config,
                    **kwargs,
                ):
                    pass
            except Exception as exc:  # noqa: BLE001
                event_session.publish_error(exc)
            finally:
                # after_agent 是正常路径；这里覆盖框架提前退出但未抛异常的情况。
                event_session.finish()
                self._close_runtime_backends()

    def _close_runtime_backends(self) -> None:
        """Close every route backend owned by this per-run agent instance."""
        if self._workspace_backend is None:
            return
        candidates = [getattr(self._workspace_backend, "default", None)]
        routes = getattr(self._workspace_backend, "routes", {}) or {}
        candidates.extend(routes.values())
        seen: set[int] = set()
        for backend in candidates:
            if backend is None or id(backend) in seen:
                continue
            seen.add(id(backend))
            close = getattr(backend, "close", None)
            if callable(close):
                close()

    def _merge_thread_config(
        self,
        config: Optional[RunnableConfig],
    ) -> Optional[RunnableConfig]:
        """补齐稳定 thread_id，同时保留调用方 callbacks/tags 等配置。"""
        account_id = str(self.agent_config.account_id or "")
        app_id = str(self.agent_config.app_id or "")
        conversation_id = str(self.agent_config.conversation_id or "")
        if not all((account_id, app_id, conversation_id)):
            return config

        defaults = build_thread_config(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
        )
        if config is None:
            return defaults

        merged: RunnableConfig = dict(defaults)
        merged.update(config)
        merged["configurable"] = {
            **defaults["configurable"],
            **(config.get("configurable") or {}),
        }
        merged["metadata"] = {
            **defaults["metadata"],
            **(config.get("metadata") or {}),
        }
        return merged

    @property
    def queue_manager(self) -> AgentQueueManager:
        return self._agent_queue_manager


__all__ = ["BaseAgent"]
