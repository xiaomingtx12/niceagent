"""运行表单的业务规则；不依赖数据库事务或 Agent Protocol 响应。"""

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any
from uuid import UUID

from internal.core.agent_v2.entity.user_input import PendingUserInput


class InvalidInputAnswer(ValueError):
    """保留表单和字段身份，由协议边界决定如何显示错误。"""

    def __init__(self, message: str, interrupt_id: str, field_name: str):
        """只携带错误定位，不记录用户答案。"""
        super().__init__(message)
        self.interrupt_id = interrupt_id
        self.field_name = field_name


@dataclass(frozen=True)
class RunAccepted:
    """已接受的业务操作及客户端可以继续读取的事件序号。"""

    run_id: UUID
    sequence: int


@dataclass(frozen=True)
class RunRejected:
    """可呈现的运行冲突或字段错误，不绑定 SDK 的响应外壳。"""

    message: str
    field_errors: dict[str, dict[str, str]] | None = None


@dataclass(frozen=True)
class RunCancellation:
    """取消操作的业务结果，由外部入口选择响应格式。"""

    cancel_requested: bool
    run_id: UUID
    status: str


class AgentRunInteraction:
    """只处理待填表单及取消恢复规则；提交记录和调用参数由持久化层管理。"""

    def __init__(self, status: str, state: Mapping[str, Any] | None):
        """读取已锁定 Run 的交互状态；规则返回新字典，不原地修改 ORM 数据。"""
        self.status = status
        self._state = dict(state or {})

    @staticmethod
    def input_batch_id(answers: Mapping[str, Any]) -> str:
        """中断 ID 集合标识一批表单，答案值和请求键顺序不影响批次身份。"""
        encoded = json.dumps(sorted(answers), ensure_ascii=False, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    def validate_answers(self, answers: Mapping[str, Any]) -> dict[str, Any]:
        """必须一次覆盖全部待填表单，未知字段和无效答案由领域表单拒绝。"""
        if self.status != "waiting_input":
            raise ValueError("当前任务没有等待填写的表单")
        pending = self._pending_inputs()
        if not pending or set(answers) != {item.interrupt_id for item in pending}:
            raise ValueError("表单已失效，请刷新后填写所有待回答问题")
        validated = {}
        for item in pending:
            try:
                validated[item.interrupt_id] = item.payload.validate_answers(answers[item.interrupt_id])
            except ValueError as exc:
                field_name = getattr(exc, "field_name", None)
                if field_name is not None:
                    raise InvalidInputAnswer(str(exc), item.interrupt_id, field_name) from exc
                raise
        return validated

    def after_submission(
        self, validated: Mapping[str, Any],
    ) -> dict[str, Any]:
        """保留已校验答案的展示投影；提交幂等事实独立保存为 Submission。"""
        history = list(self._state.get("input_history") or [])
        for item in self._pending_inputs():
            history.append({**item.model_dump(mode="json"), "answers": validated[item.interrupt_id]})
        return {
            **self._state,
            "pending_inputs": [], "input_history": history, "input_recovery": None,
        }

    def reopened_sequence(self, cancellation_sequence: int) -> int | None:
        """先核对取消批次再判断重复恢复，迟到请求不能复活后续状态。"""
        if self._state.get("input_cancellation_sequence") != cancellation_sequence:
            raise ValueError("任务状态已更新，请刷新后重新操作")
        if self._state.get("last_reopened_cancellation_sequence") == cancellation_sequence:
            return self._state["reopened_sequence"]
        if self.status != "cancelled" or not self._state.get("cancelled_inputs"):
            raise ValueError("当前任务没有可恢复的表单")
        return None

    def restored_state(self) -> dict[str, Any]:
        """恢复已取消表单；没有保留表单时拒绝产生空的等待状态。"""
        pending = [
            PendingUserInput.model_validate(item).model_dump(mode="json")
            for item in self._state.get("cancelled_inputs", [])
        ]
        if not pending:
            raise ValueError("此任务没有保留待填写内容，请创建新会话")
        return {**self._state, "pending_inputs": pending, "input_recovery": None}

    def after_reopen(
        self, restored: Mapping[str, Any], cancellation_sequence: int, sequence: int,
    ) -> dict[str, Any]:
        """恢复原表单并记住取消批次，迟到的重复请求仍返回首次结果。"""
        return {
            **restored, "cancelled_inputs": [],
            "last_reopened_cancellation_sequence": cancellation_sequence,
            "reopened_sequence": sequence,
        }

    def after_cancel(self, run_id: UUID, sequence: int) -> dict[str, Any]:
        """取消待填任务时保留原表单，恢复标识绑定最终提交的事件序号。"""
        pending = self._state.get("pending_inputs", [])
        return {
            **self._state,
            "pending_inputs": [],
            "input_recovery": {"run_id": str(run_id), "cancellation_sequence": sequence} if pending else None,
            "cancelled_inputs": pending, "input_cancellation_sequence": sequence,
        }

    def start_conflict(self) -> str | None:
        """在表单处取消后必须走恢复流程，不能用新消息跳过原有中断。"""
        if self.status != "cancelled" or not self._state.get("cancelled_inputs"):
            return None
        if self._state.get("input_recovery"):
            return "此任务已在表单处停止，请点击重新填写后继续，或创建新会话"
        return "此任务已在表单处停止，请创建新会话"

    def _pending_inputs(self) -> list[PendingUserInput]:
        """仅在提交时解析表单，不让纯查询和取消承担无关校验。"""
        return [PendingUserInput.model_validate(item) for item in self._state.get("pending_inputs", [])]
