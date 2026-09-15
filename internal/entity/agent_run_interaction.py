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


class AgentRunInteraction:
    """校验当前待填批次并生成已确认问答；提交幂等和事务由调用方管理。"""

    def __init__(
        self, status: str, pending_inputs: list[dict[str, Any]] | None,
        input_history: list[dict[str, Any]] | None,
    ):
        """读取已锁定 Run 的表单和历史；返回新列表，不原地修改 ORM JSON。"""
        self.status = status
        self._pending = list(pending_inputs or [])
        self._history = list(input_history or [])

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
    ) -> list[dict[str, Any]]:
        """保留问题和已校验答案，后续模型调用不依赖恢复已结束的图。"""
        history = list(self._history)
        for item in self._pending_inputs():
            history.append({**item.model_dump(mode="json"), "answers": validated[item.interrupt_id]})
        return history

    def _pending_inputs(self) -> list[PendingUserInput]:
        """仅在提交时解析表单，不让纯查询承担无关校验。"""
        return [PendingUserInput.model_validate(item) for item in self._pending]
