"""运行表单的业务规则；不依赖数据库事务或 Agent Protocol 响应。"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
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
    """集中维护提交幂等、待填答案和取消恢复的 JSON 契约，保留未知快照字段。"""

    def __init__(self, status: str, snapshot: Mapping[str, Any] | None):
        """读取已锁定 Run 的快照；规则只生成新快照，不原地修改 ORM 数据。"""
        self.status = status
        self._snapshot = dict(snapshot or {})
        self._interaction = dict(self._snapshot.get("_interaction") or {})

    @property
    def invocation(self) -> Any:
        """返回暂停时的调用参数；恢复身份仍由服务重新鉴权覆盖。"""
        return self._interaction.get("invocation")

    def submitted_sequence(self, answers: Mapping[str, Any]) -> int | None:
        """相同提交返回首次序号，同一批表单的不同答案不能重复提交。"""
        for submitted in self._interaction.get("submissions") or []:
            if set(submitted["answers"]) != set(answers):
                continue
            if submitted["answers"] != answers:
                raise ValueError("该表单已提交，不能重复修改")
            return submitted["sequence"]
        return None

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
        self, answers: Mapping[str, Any], validated: Mapping[str, Any], sequence: int,
    ) -> dict[str, Any]:
        """生成回答历史与去重记录；调用方将其与新执行租约原子保存。"""
        history = list(self._snapshot.get("input_history") or [])
        for item in self._pending_inputs():
            history.append({**item.model_dump(mode="json"), "answers": validated[item.interrupt_id]})
        submissions = list(self._interaction.get("submissions") or [])
        submissions.append({"answers": deepcopy(answers), "sequence": sequence})
        return {
            **self._snapshot,
            "pending_inputs": [], "input_history": history, "input_recovery": None,
            "_interaction": {**self._interaction, "submissions": submissions},
        }

    def reopened_sequence(self, cancellation_sequence: int) -> int | None:
        """先核对取消批次再判断重复恢复，迟到请求不能复活后续状态。"""
        if self._interaction.get("input_cancellation_sequence") != cancellation_sequence:
            raise ValueError("任务状态已更新，请刷新后重新操作")
        if self._interaction.get("last_reopened_cancellation_sequence") == cancellation_sequence:
            return self._interaction["reopened_sequence"]
        if self.status != "cancelled" or not self._interaction.get("cancelled_input"):
            raise ValueError("当前任务没有可恢复的表单")
        return None

    def restored_snapshot(self) -> dict[str, Any]:
        """恢复已取消表单；没有保留表单时拒绝产生空的等待状态。"""
        pending = [
            PendingUserInput.model_validate(item).model_dump(mode="json")
            for item in self._interaction.get("cancelled_inputs", [])
        ]
        if not pending:
            raise ValueError("此任务没有保留待填写内容，请创建新会话")
        return {**self._snapshot, "pending_inputs": pending, "input_recovery": None}

    def after_reopen(self, snapshot: Mapping[str, Any], cancellation_sequence: int, sequence: int) -> dict[str, Any]:
        """记录恢复批次以支持重复请求，保留原始调用和历史提交。"""
        return {**snapshot, "_interaction": {
            **self._interaction,
            "cancelled_input": False, "cancelled_inputs": [],
            "last_reopened_cancellation_sequence": cancellation_sequence,
            "reopened_sequence": sequence,
        }}

    def after_cancel(self, run_id: UUID, sequence: int) -> dict[str, Any]:
        """取消待填任务时保留原表单，恢复标识绑定最终提交的事件序号。"""
        pending = self._snapshot.get("pending_inputs", [])
        return {
            **self._snapshot,
            "pending_inputs": [],
            "input_recovery": {"run_id": str(run_id), "cancellation_sequence": sequence} if pending else None,
            "_interaction": {
                **self._interaction, "cancelled_input": True,
                "cancelled_inputs": pending, "input_cancellation_sequence": sequence,
            },
        }

    def start_conflict(self) -> str | None:
        """在表单处取消后必须走恢复流程，不能用新消息跳过原有中断。"""
        if self.status != "cancelled" or not self._interaction.get("cancelled_input"):
            return None
        if self._snapshot.get("input_recovery"):
            return "此任务已在表单处停止，请点击重新填写后继续，或创建新会话"
        return "此任务已在表单处停止，请创建新会话"

    def _pending_inputs(self) -> list[PendingUserInput]:
        """仅在提交时解析表单，不让纯查询和取消承担无关校验。"""
        return [PendingUserInput.model_validate(item) for item in self._snapshot.get("pending_inputs", [])]
