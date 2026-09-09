"""使用真实 LangGraph 检查点验证表单暂停和恢复，无模型或外部服务调用。"""

from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from contextlib import nullcontext

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.entity.user_input import UserInputForm
from internal.core.agent_v2.execution.agent_executor import AgentExecutor
from internal.core.agent_v2.observability.run_observer import PlatformAgentRunObserver
from internal.core.agent_v2.tools.ask_user import ask_user
from internal.schema.agent_workbench_schema import WorkbenchCommandReq, WorkbenchEvent, WorkbenchEventStreamReq
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector
from internal.service.agent_workbench_service import AgentWorkbenchService
from internal.service.agent_workbench_run_service import AgentWorkbenchRunService
from internal.schema.agent_runtime_schema import AgentInvocation, AgentInvocationContext
from internal.schema.agent_workbench_schema import WorkbenchInputRespondReq
from internal.entity.conversation_entity import InvokeFrom
from internal.model import Conversation, AgentRun
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from internal.core.agent_v2.tools.ask_user import UserInputMiddleware


FORM = {
    "title": "Article requirements",
    "fields": [
        {"name": "audience", "label": "Audience", "type": "select", "options": ["Beginner", "Expert"]},
        {"name": "length", "label": "Length", "type": "number"},
        {"name": "notes", "label": "Notes", "type": "textarea", "required": False},
    ],
}


class ToolCallingModel(FakeMessagesListChatModel):
    """固定工具调用以复现真实 Deep Agents 的工具暂停事件。"""

    def bind_tools(self, tools, **kwargs):
        """测试不调用供应商，但保留生产工具节点和检查点执行。"""
        return self


@pytest.mark.parametrize("invalid", [False, True])
def test_deep_agent_pause_is_not_public_tool_failure(invalid):
    """正常提问暂停不能被工作台投影成红色工具失败。"""
    model = ToolCallingModel(responses=[
        AIMessage(content="Please clarify", tool_calls=[{"name": "ask_user", "args": {} if invalid else FORM, "id": "ask-1", "type": "tool_call"}]),
        AIMessage(content="Resumed"),
    ])
    graph = create_deep_agent(model=model, middleware=[UserInputMiddleware()], checkpointer=InMemorySaver())
    context = AgentRunContext(account_id=uuid4(), app_id=uuid4(), conversation_id=uuid4(), created_by=uuid4(), principal_id=uuid4())
    observer = PlatformAgentRunObserver(uuid4())
    executor = AgentExecutor(graph=graph, runtime=SimpleNamespace(backend=None, runtime_context=context), observer=observer)
    projector = AgentProtocolEventProjector()
    events = []
    result = executor.execute({"messages": [HumanMessage(content="Write")]},
                              on_event=lambda event: events.extend(projector.project_many(event)), cancel_requested=Event())
    assert result.error is None
    if invalid:
        assert [e for e in events if e["method"] == "tools" and e["params"]["data"]["event"] == "tool-error"]
        return
    assert result.status == "waiting_input"
    assert not [e for e in events if e["method"] == "tools" and e["params"]["data"]["event"] == "tool-error"]
    assert observer.snapshot()["status"] == "waiting_input"


def test_executor_interrupt_and_resume_with_new_instance():
    """暂停释放资源，新执行器使用同一检查点接收答案并继续；前序节点不重跑。"""
    started = []
    completed = []
    checkpoint = InMemorySaver()

    def prepare(state):
        """记录已完成节点的执行次数。"""
        started.append(True)
        return {}

    def collect(state):
        """通过生产工具中断，恢复值成为工具返回值。"""
        answers = ask_user.invoke(FORM)
        completed.append(answers)
        return {"messages": [AIMessage(content=f"For {answers['audience']}: {answers['length']}")]}

    graph = (StateGraph(MessagesState).add_node("prepare", prepare).add_node("collect", collect)
             .add_edge(START, "prepare").add_edge("prepare", "collect").add_edge("collect", END)
             .compile(checkpointer=checkpoint))
    context = AgentRunContext(account_id=uuid4(), app_id=uuid4(), conversation_id=uuid4(),
                              created_by=uuid4(), principal_id=uuid4())

    def executor():
        """每次恢复重新装配资源，模拟后台线程退出后再次进入。"""
        backend = SimpleNamespace(close=Mock(), routes={})
        return AgentExecutor(graph=graph, runtime=SimpleNamespace(backend=backend, runtime_context=context),
                             observer=PlatformAgentRunObserver(uuid4())), backend

    first, backend = executor()
    result = first.execute({"messages": [HumanMessage(content="Write an article")]},
                           on_event=lambda event: None, cancel_requested=Event())
    assert result.error is None
    assert result.status == "waiting_input"
    assert len(result.interrupts) == 1
    assert result.interrupts[0]["payload"]["title"] == FORM["title"]
    backend.close.assert_called_once()
    assert completed == []
    resumed, backend = executor()
    result = resumed.execute(Command(resume={result.interrupts[0]["interrupt_id"]: {"audience": "Expert", "length": 500}}),
                             on_event=lambda event: None, cancel_requested=Event())
    assert result.error is None
    assert result.status == "completed"
    assert result.answer == "For Expert: 500"
    assert started == [True]
    assert completed == [{"audience": "Expert", "length": 500}]
    backend.close.assert_called_once()


@pytest.mark.parametrize("answers", [
    {}, {"audience": "Other", "length": 100}, {"audience": "Expert", "length": True},
    {"audience": "Expert", "length": "100"}, {"audience": "Expert", "length": float("inf")},
    {"audience": "Expert", "length": 100, "unknown": "injected"},
])
def test_reject_invalid_answers(answers):
    """服务端不信任客户端类型、选项、未知字段或必填校验。"""
    with pytest.raises(ValueError):
        UserInputForm.model_validate(FORM).validate_answers(answers)


def test_schema_supports_sdk_response_and_rejects_state_mutation():
    """真实 SDK 请求可解析，但客户端不能通过恢复接口任意改写图状态。"""
    command = WorkbenchCommandReq.from_mapping({"method": "input.respond", "params": {
        "namespace": [], "interrupt_id": "question", "response": {"length": 10},
    }})
    assert command.to_run_input().responses[0].response == {"length": 10}
    command.params["goto"] = "dangerous-node"
    with pytest.raises(Exception, match="表单提交格式错误"):
        command.to_run_input()


def test_input_event_uses_input_subscription_channel():
    """input.requested 方法归属 input 通道，普通消息订阅不能误收。"""
    event = WorkbenchEvent(type="event", event_id="run:1", seq=1, method="input.requested",
                           params={"data": {"interrupt_id": "question", "payload": FORM}})
    assert WorkbenchEventStreamReq(channels=["input"]).matches(event)
    assert not WorkbenchEventStreamReq(channels=["messages"]).matches(event)
    assert AgentProtocolEventProjector.to_public_event(event) == event


def paused_service():
    """模拟持久化会话行，复用生产服务的事务内状态转换逻辑。"""
    context = AgentInvocationContext(account_id=uuid4(), app_id=uuid4(), conversation_id=uuid4(),
                                     created_by=uuid4(), end_user_id=None, invoke_from=InvokeFrom.DEBUGGER)
    invocation = AgentInvocation(**context.model_dump(), message_id=uuid4(), query="Write", definition_fingerprint="v1")
    pending = {"interrupt_id": "question", "namespace": [], "payload": UserInputForm.model_validate(FORM).model_dump(mode="json")}
    row = SimpleNamespace(id=uuid4(), status="waiting_input", last_event_sequence=10, message_id=invocation.message_id,
                          input_message_id=None, state_snapshot={"pending_inputs": [pending], "_interaction": {
                              "invocation": invocation.model_dump(mode="json"), "submissions": [],
                          }})
    db = Mock()
    db.auto_commit.side_effect = lambda: nullcontext()
    conversation_query = Mock()
    conversation_query.filter.return_value.with_for_update.return_value.one_or_none.return_value = (context.conversation_id,)
    run_query = Mock()
    run_query.populate_existing.return_value.filter.return_value.order_by.return_value.with_for_update.return_value.first.return_value = row
    db.session.query.side_effect = lambda target: run_query if target is AgentRun else conversation_query
    service = AgentWorkbenchService(db=db, run_service=Mock(), queue_manager=Mock(), execution_service=Mock(),
                                     attachment_service=Mock(), conversation_service=Mock())
    service.queue_manager.get.return_value = None
    service.execution_service.agent_assembly_service.get_definition_fingerprint.return_value = "v1"
    service._launch_run = Mock()
    response = WorkbenchInputRespondReq.model_validate({"namespace": [], "interrupt_id": "question",
                                                       "response": {"audience": "Expert", "length": 500}})
    return service, row, context, response


def test_resume_claim_is_idempotent_and_retains_form_history():
    """HTTP 重试返回同一结果，不能再次启动线程或重复追加历史。"""
    service, row, context, response = paused_service()
    first = service.respond_input(context=context, response=response)
    second = service.respond_input(context=context, response=response)
    assert first == second
    assert row.status == "queued"
    assert row.state_snapshot["pending_inputs"] == []
    assert len(row.state_snapshot["input_history"]) == 1
    assert len(row.state_snapshot["_interaction"]["submissions"]) == 1
    service._launch_run.assert_called_once()
    assert service._launch_run.call_args.args[0].resume == {"question": {"audience": "Expert", "length": 500}}


@pytest.mark.parametrize("case", ["changed_answer", "stale_id", "changed_config", "missing_conversation"])
def test_resume_rejects_invalid_or_stale_requests_without_launch(case):
    """过期表单、配置漂移、越权会话和重复修改不能恢复执行。"""
    service, row, context, response = paused_service()
    if case == "changed_answer":
        service.respond_input(context=context, response=response)
        service._launch_run.reset_mock()
        response.responses[0].response["length"] = 700
    elif case == "stale_id":
        response.responses[0].interrupt_id = "old-question"
    elif case == "changed_config":
        service.execution_service.agent_assembly_service.get_definition_fingerprint.return_value = "v2"
    else:
        service.db.session.query(Conversation.id).filter.return_value.with_for_update.return_value.one_or_none.return_value = None
    with pytest.raises(Exception):
        service.respond_input(context=context, response=response)
    service._launch_run.assert_not_called()


def test_cancel_waiting_input_is_terminal_without_background_worker():
    """暂停时没有工作线程，停止请求必须直接持久化取消并清除卡片。"""
    service, row, _, _ = paused_service()
    query = Mock()
    query.populate_existing.return_value.with_for_update.return_value.filter.return_value.one_or_none.return_value = row
    service.db.session.query.return_value = query
    service.db.session.query.side_effect = None
    run_service = AgentWorkbenchRunService(db=service.db)
    assert run_service.request_cancel(row)
    assert row.status == "cancelled"
    assert row.state_snapshot["pending_inputs"] == []
    assert row.finished_at is not None
    events = [call.args[0].payload for call in service.db.session.add.call_args_list]
    assert events[-1]["params"]["data"]["status"] == "cancelled"
    assert events[0]["params"]["data"]["pending_inputs"] == []
