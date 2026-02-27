import json
import logging
import re
import time
import uuid
from typing import Literal

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
    AIMessage,
    RemoveMessage,
    messages_to_dict,
)
from langgraph.constants import END
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from internal.core.agent.entities.agent_entity import (
    AgentState,
    AGENT_SYSTEM_PROMPT_TEMPLATE,
    MAX_ITERATION_RESPONSE,
    DATASET_RETRIEVAL_TOOL_NAME,
)
from internal.core.language_model.utils.token_counter import TokenCounter
from internal.exception import FailException
from .base_agent import BaseAgent
from ..entities.queue_entity import AgentThought, QueueEvent


class FunctionCallAgent(BaseAgent):
    """基于函数/工具调用的智能体"""

    def _build_agent(self) -> CompiledStateGraph:
        """构建LangGraph图结构编译程序"""
        # 1.创建图
        graph = StateGraph(AgentState)  # type: ignore

        # 2.添加节点
        graph.add_node("preset_operation", self._preset_operation_node)
        graph.add_node("long_term_memory_recall", self._long_term_memory_recall_node)
        graph.add_node("llm", self._llm_node)
        graph.add_node("tools", self._tools_node)

        # 3.添加边，并设置起点和终点
        graph.set_entry_point("preset_operation")
        graph.add_conditional_edges(
            "preset_operation", self._preset_operation_condition
        )
        graph.add_edge("long_term_memory_recall", "llm")
        graph.add_conditional_edges("llm", self._tools_condition)
        graph.add_edge("tools", "llm")

        # 4.编译应用并返回
        agent = graph.compile()

        return agent

    def _preset_operation_node(self, state: AgentState) -> AgentState:
        """预设操作，涵盖：输入审核、数据预处理、条件边等"""
        # 1.获取审核配置与用户输入query
        review_config = self.agent_config.review_config
        query = state["messages"][-1].content

        # 2.检测是否开启审核配置
        if review_config["enable"] and review_config["inputs_config"]["enable"]:
            contains_keyword = any(
                keyword in query for keyword in review_config["keywords"]
            )
            # 3.如果包含敏感词则执行后续步骤
            if contains_keyword:
                preset_response = review_config["inputs_config"]["preset_response"]
                self.agent_queue_manager.publish(
                    state["task_id"],
                    AgentThought(
                        id=uuid.uuid4(),
                        task_id=state["task_id"],
                        event=QueueEvent.AGENT_MESSAGE,
                        thought=preset_response,
                        message=messages_to_dict(state["messages"]),
                        answer=preset_response,
                        latency=0,
                    ),
                )
                self.agent_queue_manager.publish(
                    state["task_id"],
                    AgentThought(
                        id=uuid.uuid4(),
                        task_id=state["task_id"],
                        event=QueueEvent.AGENT_END,
                    ),
                )
                return {"messages": [AIMessage(preset_response)]}

        return {"messages": []}

    def _long_term_memory_recall_node(self, state: AgentState) -> AgentState:
        """长期记忆召回节点（LangChain 1.0 版本）

        处理流程：
        1. 获取长期记忆并发布事件
        2. 构建系统消息（包含预设提示和长期记忆）
        3. 检查并处理现有消息历史
        4. 使用 RemoveMessage 删除旧消息，确保消息顺序正确
        """
        # 1.根据传递的智能体配置判断是否需要召回长期记忆
        long_term_memory = ""
        if self.agent_config.enable_long_term_memory:
            long_term_memory = state.get("long_term_memory", "")
            if long_term_memory:
                self.agent_queue_manager.publish(
                    state["task_id"],
                    AgentThought(
                        id=uuid.uuid4(),
                        task_id=state["task_id"],
                        event=QueueEvent.LONG_TERM_MEMORY_RECALL,
                        observation=long_term_memory,
                    ),
                )

        # 2.构建系统消息，包含预设提示和长期记忆
        system_message = SystemMessage(
            AGENT_SYSTEM_PROMPT_TEMPLATE.format(
                preset_prompt=self.agent_config.preset_prompt,
                long_term_memory=long_term_memory,
            )
        )

        # 3.获取当前消息列表
        messages = state["messages"]
        current_message = messages[-1]  # 当前用户消息

        # 调试：打印当前消息列表的类型
        logging.info(
            f"[DEBUG] _long_term_memory_recall_node 接收到的消息类型: {[msg.type for msg in messages]}"
        )
        logging.info(f"[DEBUG] _long_term_memory_recall_node 消息数量: {len(messages)}")

        # 4.提取历史对话消息（排除当前消息，只保留人类和AI的对话）
        # 同时收集所有需要删除的旧消息ID
        history_messages = []
        messages_to_remove = []

        for msg in messages[:-1]:  # 排除当前消息
            messages_to_remove.append(RemoveMessage(id=msg.id))  # 标记删除
            if msg.type in ["human", "ai"]:
                history_messages.append(msg)

        # 5.校验历史消息格式（应该是成对的：人类消息 -> AI消息）
        if len(history_messages) > 0 and len(history_messages) % 2 != 0:
            self.agent_queue_manager.publish_error(
                state["task_id"], "智能体历史消息列表格式错误"
            )
            logging.exception(
                f"智能体历史消息列表格式错误, len(history)={len(history_messages)}, history={json.dumps(messages_to_dict(history_messages), ensure_ascii=False)}"
            )
            raise FailException("智能体历史消息列表格式错误")

        # 6.构建新的消息列表：系统消息 -> 历史对话 -> 当前用户消息
        new_messages = [system_message]
        new_messages.extend(history_messages)
        new_messages.append(HumanMessage(current_message.content))

        # 调试：打印构建的新消息列表顺序
        logging.info(
            f"[DEBUG] 构建的新消息列表顺序: {[msg.type for msg in new_messages]}"
        )

        # 7.返回更新：先删除所有旧消息，再添加新消息
        # 这确保了消息列表的顺序正确：系统消息始终在第一位
        return {
            "messages": [
                RemoveMessage(id=current_message.id),  # 删除当前消息
                *messages_to_remove,  # 删除所有旧消息
                *new_messages,  # 添加新消息列表
            ],
        }

    def _llm_node(self, state: AgentState) -> AgentState:
        """大语言模型节点"""
        # 1.检测当前Agent迭代次数是否符合需求
        if state["iteration_count"] > self.agent_config.max_iteration_count:
            self.agent_queue_manager.publish(
                state["task_id"],
                AgentThought(
                    id=uuid.uuid4(),
                    task_id=state["task_id"],
                    event=QueueEvent.AGENT_MESSAGE,
                    thought=MAX_ITERATION_RESPONSE,
                    message=messages_to_dict(state["messages"]),
                    answer=MAX_ITERATION_RESPONSE,
                    latency=0,
                ),
            )
            self.agent_queue_manager.publish(
                state["task_id"],
                AgentThought(
                    id=uuid.uuid4(),
                    task_id=state["task_id"],
                    event=QueueEvent.AGENT_END,
                ),
            )
            return {"messages": [AIMessage(MAX_ITERATION_RESPONSE)]}

        # 2.从智能体配置中提取大语言模型
        id = uuid.uuid4()
        start_at = time.perf_counter()
        llm = self.llm

        # 调试：打印 _llm_node 接收到的消息顺序
        # logging.info(f"[DEBUG] _llm_node 接收到的消息顺序: {[msg.type for msg in state['messages']]}")
        # logging.info(f"[DEBUG] _llm_node 消息数量: {len(state['messages'])}")
        if state["messages"]:
            logging.info(
                f"[DEBUG] _llm_node 第一条消息类型: {state['messages'][0].type}"
            )

        # 3.确保系统消息在第一位（防御性编程）
        messages = state["messages"]
        system_msg = None
        other_messages = []

        for msg in messages:
            if msg.type == "system":
                if system_msg is None:  # 只保留第一个系统消息
                    system_msg = msg
            else:
                other_messages.append(msg)

        # 重新组织消息：系统消息在最前面
        if system_msg:
            ordered_messages = [system_msg] + other_messages
        else:
            ordered_messages = other_messages

        logging.info(
            f"[DEBUG] _llm_node 重新排序后的消息顺序: {[msg.type for msg in ordered_messages]}"
        )

        # 3.检测大语言模型实例是否有bind_tools方法，如果没有则不绑定，如果有还需要检测tools是否为空，不为空则绑定
        if (
            hasattr(llm, "bind_tools")
            and callable(getattr(llm, "bind_tools"))
            and len(self.agent_config.tools) > 0
        ):
            try:
                llm = llm.bind_tools(self.agent_config.tools)
            except NotImplementedError:
                logging.warning(
                    f"当前模型不支持bind_tools（function calling），将以不绑定工具的方式运行"
                )

        # 4.流式调用LLM输出对应内容
        gathered = None
        generation_type = ""
        accumulated_answer = ""  # 累积完整答案

        try:
            for chunk in llm.stream(ordered_messages):  # 使用重新排序的消息
                # 累积完整响应
                if gathered is None:
                    gathered = chunk
                else:
                    gathered += chunk
                
                # 5.检测生成类型是工具参数还是文本生成（只需判断一次）
                if not generation_type:
                    if chunk.tool_calls:
                        generation_type = "thought"
                    elif chunk.content:
                        generation_type = "message"

                # 6.如果生成的是消息则提交智能体消息事件
                if generation_type == "message" and chunk.content:
                    # 7.提取片段内容并检测是否开启输出审核
                    review_config = self.agent_config.review_config
                    content = chunk.content
                    if (
                        review_config["enable"]
                        and review_config["outputs_config"]["enable"]
                    ):
                        for keyword in review_config["keywords"]:
                            content = re.sub(
                                re.escape(keyword),
                                "**",
                                content,
                                flags=re.IGNORECASE,
                            )

                    # 累积答案内容
                    accumulated_answer += content

                    self.agent_queue_manager.publish(
                        state["task_id"],
                        AgentThought(
                            id=id,
                            task_id=state["task_id"],
                            event=QueueEvent.AGENT_MESSAGE,
                            thought=content,
                            message=messages_to_dict(state["messages"]),
                            answer=content,
                            latency=(time.perf_counter() - start_at),
                        ),
                    )
        except Exception as e:
            logging.exception(f"LLM节点发生错误, 错误信息: {str(e)}")
            self.agent_queue_manager.publish_error(
                state["task_id"], f"LLM节点发生错误, 错误信息: {str(e)}"
            )
            raise e

        # 8.计算LLM的输入+输出token总数（使用精确计数）
        model_name = TokenCounter.get_model_name_from_llm(self.llm)
        input_token_count = TokenCounter.count_messages_tokens(
            state["messages"], model_name
        )
        output_token_count = TokenCounter.count_messages_tokens([gathered], model_name)

        # 9.获取输入/输出价格和单位
        input_price, output_price, unit = self.llm.get_pricing()

        # 10.计算总token+总成本
        total_token_count = input_token_count + output_token_count
        total_price = (
            input_token_count * input_price + output_token_count * output_price
        ) * unit
        # 11.如果类型为推理则添加智能体推理事件
        if generation_type == "thought":
            self.agent_queue_manager.publish(
                state["task_id"],
                AgentThought(
                    id=id,
                    task_id=state["task_id"],
                    event=QueueEvent.AGENT_THOUGHT,
                    thought=json.dumps(gathered.tool_calls, ensure_ascii=False),
                    # 消息相关字段
                    message=messages_to_dict(state["messages"]),
                    message_token_count=input_token_count,
                    message_unit_price=input_price,
                    message_price_unit=unit,
                    # 答案相关字段
                    answer="",
                    answer_token_count=output_token_count,
                    answer_unit_price=output_price,
                    answer_price_unit=unit,
                    # Agent推理统计相关
                    total_token_count=total_token_count,
                    total_price=total_price,
                    latency=(time.perf_counter() - start_at),
                ),
            )
        elif generation_type == "message":
            # 12.如果LLM直接生成answer则表示已经拿到了最终答案
            # 发送最终的统计信息（不包含answer，避免重复）
            self.agent_queue_manager.publish(
                state["task_id"],
                AgentThought(
                    id=id,
                    task_id=state["task_id"],
                    event=QueueEvent.AGENT_MESSAGE,
                    thought="",
                    # 消息相关字段
                    message=messages_to_dict(state["messages"]),
                    message_token_count=input_token_count,
                    message_unit_price=input_price,
                    message_price_unit=unit,
                    # 答案相关字段（空字符串，避免重复显示）
                    answer="",
                    answer_token_count=output_token_count,
                    answer_unit_price=output_price,
                    answer_price_unit=unit,
                    # Agent推理统计相关
                    total_token_count=total_token_count,
                    total_price=total_price,
                    latency=(time.perf_counter() - start_at),
                ),
            )
            # 发送结束事件
            self.agent_queue_manager.publish(
                state["task_id"],
                AgentThought(
                    id=uuid.uuid4(),
                    task_id=state["task_id"],
                    event=QueueEvent.AGENT_END,
                ),
            )

        return {"messages": [gathered], "iteration_count": state["iteration_count"] + 1}

    def _tools_node(self, state: AgentState) -> AgentState:
        """工具执行节点"""
        # 1.将工具列表转换成字典，便于调用指定的工具
        tools_by_name = {tool.name: tool for tool in self.agent_config.tools}

        # 2.提取消息中的工具调用参数
        tool_calls = state["messages"][-1].tool_calls

        # 3.循环执行工具组装工具消息
        messages = []
        for tool_call in tool_calls:
            # 4.创建智能体动作事件id并记录开始时间
            id = uuid.uuid4()
            start_at = time.perf_counter()

            try:
                # 5.获取工具并调用工具
                tool = tools_by_name[tool_call["name"]]
                tool_result = tool.invoke(tool_call["args"])
            except Exception as e:
                # 6.添加错误工具信息
                tool_result = f"工具执行出错: {str(e)}"

            # 7.将工具消息添加到消息列表中
            messages.append(
                ToolMessage(
                    tool_call_id=tool_call["id"],
                    content=json.dumps(tool_result, ensure_ascii=False),
                    name=tool_call["name"],
                )
            )

            # 7.判断执行工具的名字，提交不同事件，涵盖智能体动作以及知识库检索
            event = (
                QueueEvent.AGENT_ACTION
                if tool_call["name"] != DATASET_RETRIEVAL_TOOL_NAME
                else QueueEvent.DATASET_RETRIEVAL
            )
            self.agent_queue_manager.publish(
                state["task_id"],
                AgentThought(
                    id=id,
                    task_id=state["task_id"],
                    event=event,
                    observation=json.dumps(tool_result, ensure_ascii=False),
                    tool=tool_call["name"],
                    tool_input=tool_call["args"],
                    latency=(time.perf_counter() - start_at),
                ),
            )

        return {"messages": messages}

    @classmethod
    def _tools_condition(cls, state: AgentState) -> Literal["tools", "__end__"]:
        """检测下一个节点是执行tools节点，还是直接结束"""
        # 1.提取状态中的最后一条消息(AI消息)
        messages = state["messages"]
        ai_message = messages[-1]

        # 2.检测是否存在tools_calls这个参数，如果存在则执行tools节点，否则结束
        if hasattr(ai_message, "tool_calls") and len(ai_message.tool_calls) > 0:
            return "tools"

        return END

    @classmethod
    def _preset_operation_condition(
        cls, state: AgentState
    ) -> Literal["long_term_memory_recall", "__end__"]:
        """预设操作条件边，用于判断是否触发预设响应"""
        # 1.提取状态的最后一条消息
        message = state["messages"][-1]

        # 2.判断消息的类型，如果是AI消息则说明触发了审核机制，直接结束
        if message.type == "ai":
            return END

        return "long_term_memory_recall"
