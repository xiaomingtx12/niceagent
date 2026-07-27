import uuid
from threading import Thread
from typing import Optional, Any, Iterator

from langchain_core.language_models import BaseChatModel
from langchain_core.load import Serializable
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from pydantic import ConfigDict, PrivateAttr

from internal.core.agent.entities.agent_entity import AgentConfig, AgentState
from internal.core.agent_v2.factory import build_deep_agent, build_thread_config
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

    model_config = ConfigDict(
        # 字段允许接收任意类型，且不需要校验器
        arbitrary_types_allowed=True
    )

    name: str = "base_agent"

    def __init__(
        self,
        llm: BaseChatModel,
        agent_config: AgentConfig,
        *args,
        **kwargs,
    ):
        """构造函数，调用 build_deep_agent 构造 CompiledStateGraph。"""
        super().__init__(*args, llm=llm, agent_config=agent_config, **kwargs)
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

        R1 阶段默认走 PostgresSaver（use_checkpointer=True）；单测里 chat_service
        可传 `use_checkpointer=False` 跳过 DB（这一开关暂保留在 factory）。

        workspace_backend 构造方式 (R1):
        - 若 AgentConfig.enable_workspace=True + 三 ID 齐全, 构造 MyDifyWorkspaceBackend
          注入 FilesystemMiddleware; 否则不挂
        - session 从 BaseAgent 调用方注入 (chat_service 拿 flask g.sqlalchemy
          session); R1 阶段 BaseAgent 暂不持有 session, 默认 None (singleton
          build 阶段不挂 workspace; service 层 invoke 时再传)
        """
        return build_deep_agent(
            llm=self.llm,
            tools=self.agent_config.tools,
            system_prompt=self.agent_config.system_prompt.format(
                preset_prompt=self.agent_config.preset_prompt
            ),
            review_config=self.agent_config.review_config,
            account_id=str(self.agent_config.account_id or ""),
            app_id=str(self.agent_config.app_id or ""),
            conversation_id=str(self.agent_config.conversation_id or ""),
            use_checkpointer=False,  # R0 阶段先 false，等 saver migration 上线再开
            workspace_backend=None,  # R1.4 阶段; service 层 invoke 时可注入
            max_message_count=self.agent_config.max_message_count,  # R2 压缩阈值
            subagents=self.agent_config.subagents,  # R3 sub-agent 配置
        )

    def invoke(
        self, input: AgentState, config: Optional[RunnableConfig] = None
    ) -> AgentResult:
        """块内容响应，一次性生成完整内容后返回"""
        # 1.调用stream方法获取流式事件输出数据
        content = input["messages"][0].content
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
        """流式输出。

        R0 改造点：
        1. 不再走 master 老 `_agent.invoke` + AgentQueueManager 异步线程
           协议（master 老的 sub-thread 监听 model queue 在 deepagents 模型下
           拆开了 model 调用的中间件流，_agent_queue_manager 兼容性保留但
           实际机制变更：现在 thread 调 deepagent `agent.stream()` 而非
           `agent.invoke()`，把中间件事件直接通过 yield 暴露）
        2. `_agent_queue_manager.listen(task_id)` 在 R0 阶段仍然保留接口，
           但内部要改成读 deepagents stream events（不在 R0 必做范围）
        3. R0 最小改动：调 deepagents `agent.stream(input, config)`，把
           `agent_thoughts` 状态切片 yield 给上层 SSE 端点

        简化策略：R0 阶段保留 thread + queue 模式，但 thread 内调 deepagent
        stream，把 EventTranslationMiddleware 写入 state 的 agent_thoughts
        一次性拷贝到 queue，queue 端按 listen 协议 yield。
        """
        # 1.检测 agent 是否已构建
        if not self._agent:
            raise FailException("智能体未成功构建，请核实后尝试")

        # 2.构建对应的任务id及数据初始化
        input["task_id"] = input.get("task_id", uuid.uuid4())
        input["iteration_count"] = input.get("iteration_count", 0)

        # 3.创建子线程执行 deepagent stream
        # 注：thread 兼容 master 老协议——chat_service 依赖
        # `yield from self._agent_queue_manager.listen(input["task_id"])`
        # 拿到 AgentThought 流；thread 内调 deepagent stream 并把 thoughts
        # 推到 queue_manager。详见 agent_queue_manager.put_thoughts_from_deepagent
        # 的实现（R0 阶段用最小实现——单测覆盖）。
        thread = Thread(
            target=self._run_stream_in_thread,
            args=(input, config),
            kwargs=kwargs,
        )
        thread.daemon = True
        thread.start()

        # 4.调用队列管理器监听数据并返回迭代器
        yield from self._agent_queue_manager.listen(input["task_id"])

    def _run_stream_in_thread(
        self,
        input: AgentState,
        config: Optional[RunnableConfig],
        **kwargs: Any,
    ) -> None:
        """线程入口：跑 deepagent stream，把 thoughts 推到 queue_manager。

        R0 最小实现：deepagents 0.6.x `CompiledStateGraph.stream()` 返回
        `Iterator[dict[str, Any]]`，每个 chunk 是 `{node_name: state_delta}`
        —— 不是 `(mode, chunk)` 元组（tuple 是 langgraph 0.x 旧语义）。

        EventTranslationMiddleware 把累积的 thoughts 写到 state['agent_thoughts']，
        这里在 stream 跑完后一次性 publish 给 queue manager。queue 端按
        listen 协议 yield 给上层 SSE 端点。

        真实 streaming token yield 留 R1+ 由 deepagents 0.7 streaming
        middleware 提供；R0 阶段一次完整快照已足够前端展示 agent 推理步骤。
        """
        try:
            # chunk 是 dict {node_name: state_delta}，不是 (mode, chunk) 元组
            for chunk in self._agent.stream(input, config=config, **kwargs):
                if not isinstance(chunk, dict):
                    continue
                for state_delta in chunk.values():
                    if not isinstance(state_delta, dict):
                        continue
                    thoughts = state_delta.get("agent_thoughts", [])
                    for thought in thoughts:
                        self._agent_queue_manager.publish(
                            input["task_id"], thought
                        )
        except Exception as exc:  # noqa: BLE001
            self._agent_queue_manager.publish_error(input["task_id"], str(exc))

    @property
    def queue_manager(self) -> AgentQueueManager:
        return self._agent_queue_manager


__all__ = ["BaseAgent"]