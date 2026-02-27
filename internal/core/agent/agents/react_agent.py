import json
import logging
import re
import time
import uuid

from langchain_core.messages import (
    SystemMessage,
    messages_to_dict,
    HumanMessage,
    AIMessage,
    RemoveMessage,
)
from langchain_core.tools import render_text_description_and_args

from internal.core.agent.entities.agent_entity import (
    AgentState,
    AGENT_SYSTEM_PROMPT_TEMPLATE,
    REACT_AGENT_SYSTEM_PROMPT_TEMPLATE,
    MAX_ITERATION_RESPONSE,
)
from internal.core.agent.entities.queue_entity import QueueEvent, AgentThought
from internal.core.language_model.entities.model_entity import ModelFeature
from internal.core.language_model.utils.token_counter import TokenCounter
from internal.exception import FailException
from .function_call_agent import FunctionCallAgent


class ReACTAgent(FunctionCallAgent):
    """基于ReACT推理的智能体，继承FunctionCallAgent，并重写long_term_memory_node和llm_node两个节点"""

    def _long_term_memory_recall_node(self, state: AgentState) -> AgentState:
        """重写长期记忆召回节点，使用prompt实现工具调用及规范数据生成（优化版）"""
        # 1.判断是否支持工具调用，如果支持工具调用，则可以直接使用工具智能体的长期记忆召回节点
        if ModelFeature.TOOL_CALL in self.llm.features:
            return super()._long_term_memory_recall_node(state)

        # 2.根据传递的智能体配置判断是否需要召回长期记忆
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

        # 3.检测是否支持AGENT_THOUGHT，如果不支持，则使用没有工具描述的prompt
        if ModelFeature.AGENT_THOUGHT not in self.llm.features:
            system_message = SystemMessage(
                AGENT_SYSTEM_PROMPT_TEMPLATE.format(
                    preset_prompt=self.agent_config.preset_prompt,
                    long_term_memory=long_term_memory,
                )
            )
        else:
            # 4.支持智能体推理，则使用REACT_AGENT_SYSTEM_PROMPT_TEMPLATE并添加工具描述
            system_message = SystemMessage(
                REACT_AGENT_SYSTEM_PROMPT_TEMPLATE.format(
                    preset_prompt=self.agent_config.preset_prompt,
                    long_term_memory=long_term_memory,
                    tool_description=render_text_description_and_args(
                        self.agent_config.tools
                    ),
                )
            )

        # 5.获取当前消息列表
        messages = state["messages"]
        current_message = messages[-1]  # 当前用户消息
        
        # 6.提取历史对话消息（排除当前消息，只保留人类和AI的对话）
        # 同时收集所有需要删除的旧消息ID
        history_messages = []
        messages_to_remove = []
        
        for msg in messages[:-1]:  # 排除当前消息
            messages_to_remove.append(RemoveMessage(id=msg.id))  # 标记删除
            if msg.type in ["human", "ai"]:
                history_messages.append(msg)
        
        # 7.校验历史消息格式（应该是成对的：人类消息 -> AI消息）
        if len(history_messages) > 0 and len(history_messages) % 2 != 0:
            self.agent_queue_manager.publish_error(
                state["task_id"], "智能体历史消息列表格式错误"
            )
            logging.exception(
                f"智能体历史消息列表格式错误, len(history)={len(history_messages)}, history={json.dumps(messages_to_dict(history_messages), ensure_ascii=False)}"
            )
            raise FailException("智能体历史消息列表格式错误")

        # 8.构建新的消息列表：系统消息 -> 历史对话 -> 当前用户消息
        new_messages = [system_message]
        new_messages.extend(history_messages)
        new_messages.append(HumanMessage(current_message.content))

        # 9.返回更新：先删除所有旧消息，再添加新消息
        # 这确保了消息列表的顺序正确：系统消息始终在第一位
        return {
            "messages": [
                RemoveMessage(id=current_message.id),  # 删除当前消息
                *messages_to_remove,  # 删除所有旧消息
                *new_messages,  # 添加新消息列表
            ],
        }

    def _llm_node(self, state: AgentState) -> AgentState:
        """重写工具调用智能体的LLM节点"""
        # 1.判断当前LLM是否支持tool_call，如果是则使用FunctionCallAgent的_llm_node
        if ModelFeature.TOOL_CALL in self.llm.features:
            return super()._llm_node(state)

        # 2.检测当前Agent迭代次数是否符合需求
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

        # 3.从智能体配置中提取大语言模型
        id = uuid.uuid4()
        start_at = time.perf_counter()
        llm = self.llm
        
        # 3.1 确保系统消息在第一位（防御性编程）
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

        # 4.定义变量存储流式输出内容
        gathered = None
        generation_type = ""
        accumulated_answer = ""  # 累积完整答案

        # 5.流式输出调用LLM，并判断输出内容是否以"```json"为开头，用于区分工具调用和文本生成
        for chunk in llm.stream(ordered_messages):  # 使用重新排序的消息
            # 6.处理流式输出内容块叠加
            if gathered is None:
                gathered = chunk
            else:
                gathered += chunk

            # 7.检测生成的类型是工具调用还是文本生成
            if not generation_type:
                # 8.当生成内容的长度大于等于7(```json)长度时才可以判断出类型是什么
                if len(gathered.content.strip()) >= 7:
                    if gathered.content.strip().startswith("```json"):
                        generation_type = "thought"
                    else:
                        generation_type = "message"

            # 9.如果生成的是消息则提交智能体消息事件
            if generation_type == "message" and chunk.content:
                # 10.提取片段内容并校测是否开启输出审核
                review_config = self.agent_config.review_config
                content = chunk.content
                if (
                    review_config["enable"]
                    and review_config["outputs_config"]["enable"]
                ):
                    for keyword in review_config["keywords"]:
                        content = re.sub(
                            re.escape(keyword), "**", content, flags=re.IGNORECASE
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
        # 8.计算LLM的输入+输出token总数（使用精确计数）
        model_name = TokenCounter.get_model_name_from_llm(self.llm)
        input_token_count = TokenCounter.count_messages_tokens(state["messages"], model_name)
        output_token_count = TokenCounter.count_messages_tokens([gathered], model_name)

        # 9.获取输入/输出价格和单位
        input_price, output_price, unit = self.llm.get_pricing()

        # 10.计算总token+总成本
        total_token_count = input_token_count + output_token_count
        total_price = (
            input_token_count * input_price + output_token_count * output_price
        ) * unit

        # 12.如果类型为推理则解析json，并添加智能体消息
        if generation_type == "thought":
            try:
                # 13.使用正则解析信息，如果失败则当成普通消息返回
                pattern = r"^```json(.*?)```$"
                matches = re.findall(pattern, gathered.content, re.DOTALL)
                match_json = json.loads(matches[0])
                tool_calls = [
                    {
                        "id": str(uuid.uuid4()),
                        "type": "tool_call",
                        "name": match_json.get("name", ""),
                        "args": match_json.get("args", {}),
                    }
                ]
                self.agent_queue_manager.publish(
                    state["task_id"],
                    AgentThought(
                        id=id,
                        task_id=state["task_id"],
                        event=QueueEvent.AGENT_THOUGHT,
                        thought=json.dumps(tool_calls, ensure_ascii=False),
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
                return {
                    "messages": [AIMessage(content="", tool_calls=tool_calls)],
                    "iteration_count": state["iteration_count"] + 1,
                }
            except Exception as _:
                generation_type = "message"
                # 解析失败，使用完整内容作为答案
                accumulated_answer = gathered.content
                self.agent_queue_manager.publish(
                    state["task_id"],
                    AgentThought(
                        id=id,
                        task_id=state["task_id"],
                        event=QueueEvent.AGENT_MESSAGE,
                        thought=gathered.content,
                        message=messages_to_dict(state["messages"]),
                        answer=gathered.content,
                        latency=(time.perf_counter() - start_at),
                    ),
                )

        # 14.如果最终类型是message则表示已经拿到最终答案
        # 发送最终的统计信息（包含完整答案）
        if generation_type == "message":
            self.agent_queue_manager.publish(
                state["task_id"],
                AgentThought(
                    id=uuid.uuid4(),
                    task_id=state["task_id"],
                    event=QueueEvent.AGENT_MESSAGE,
                    thought="",
                    # 消息相关字段
                    message=messages_to_dict(state["messages"]),
                    message_token_count=input_token_count,
                    message_unit_price=input_price,
                    message_price_unit=unit,
                    # 答案相关字段（包含完整答案）
                    answer=accumulated_answer,
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
