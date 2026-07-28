"""L1/L3 application context assembly."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest

from internal.core.agent_v2.context import build_dynamic_context


class ProjectContextMiddleware(AgentMiddleware):
    """Keep stable identity in L1 and append dynamic runtime/facts as L3."""

    state_schema = Any

    def __init__(
        self,
        *,
        account_id: str,
        app_id: str,
        conversation_id: str,
        end_user_id: str | None = None,
        workflow_variables: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._account_id = account_id
        self._app_id = app_id
        self._conversation_id = conversation_id
        self._end_user_id = end_user_id
        self._workflow_variables = dict(workflow_variables or {})

    def before_agent(self, state: dict, runtime: Any) -> dict:
        invoked_at = datetime.now(timezone.utc).isoformat()
        return {
            "runtime_account_id": self._account_id,
            "runtime_app_id": self._app_id,
            "runtime_conversation_id": self._conversation_id,
            "runtime_end_user_id": self._end_user_id,
            "runtime_invoked_at": invoked_at,
            "runtime_workflow_variables": dict(self._workflow_variables),
        }

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable,
    ):
        dynamic = build_dynamic_context(
            invoked_at=request.state.get("runtime_invoked_at")
            or datetime.now(timezone.utc).isoformat(),
            account_id=self._account_id,
            end_user_id=self._end_user_id,
            workflow_variables=request.state.get("runtime_workflow_variables")
            or self._workflow_variables,
        )
        return handler(
            request.override(
                system_message=append_to_system_message(
                    request.system_message,
                    dynamic,
                )
            )
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable,
    ):
        dynamic = build_dynamic_context(
            invoked_at=request.state.get("runtime_invoked_at")
            or datetime.now(timezone.utc).isoformat(),
            account_id=self._account_id,
            end_user_id=self._end_user_id,
            workflow_variables=request.state.get("runtime_workflow_variables")
            or self._workflow_variables,
        )
        return await handler(
            request.override(
                system_message=append_to_system_message(
                    request.system_message,
                    dynamic,
                )
            )
        )


__all__ = ["ProjectContextMiddleware"]
