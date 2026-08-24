"""HTTP endpoints for the Agent Protocol workbench."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from flask import Response, jsonify, request, stream_with_context
from flask_login import current_user, login_required
from injector import inject

from internal.service import AgentWorkbenchService


@inject
@dataclass
class AgentWorkbenchHandler:
    service: AgentWorkbenchService

    @staticmethod
    def _body() -> dict[str, Any]:
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _stream_body(cls) -> dict[str, Any]:
        """Merge the protocol cursor header into the subscription body."""
        body = cls._body()
        if "since" not in body:
            last_event_id = request.headers.get("Last-Event-ID")
            if last_event_id:
                try:
                    body["since"] = max(0, int(last_event_id.rsplit(":", 1)[-1]))
                except ValueError:
                    # An invalid cursor is treated as a fresh subscription.
                    pass
        return body

    @login_required
    def creator_command(self, app_id: UUID, thread_id: UUID):
        return jsonify(self.service.command(
            kind="app", scope=str(app_id), thread_id=thread_id,
            account=current_user, command=self._body(),
        ))

    @login_required
    def creator_stream(self, app_id: UUID, thread_id: UUID):
        body = self._stream_body()
        self.service.authorize(
            kind="app", scope=str(app_id), thread_id=thread_id, account=current_user,
        )
        events = self.service.stream(
            kind="app", scope=str(app_id), thread_id=thread_id, body=body,
        )
        return self._sse(events)

    @login_required
    def creator_state(self, app_id: UUID, thread_id: UUID):
        self.service.authorize(
            kind="app", scope=str(app_id), thread_id=thread_id, account=current_user,
        )
        return jsonify(self.service.state(kind="app", scope=str(app_id), thread_id=thread_id))

    @login_required
    def creator_messages(self, app_id: UUID, thread_id: UUID):
        self.service.authorize(
            kind="app", scope=str(app_id), thread_id=thread_id, account=current_user,
        )
        return jsonify(self._message_page(thread_id))

    @login_required
    def published_command(self, token: str, thread_id: UUID):
        return jsonify(self.service.command(
            kind="web_app", scope=token, thread_id=thread_id,
            account=current_user, command=self._body(),
        ))

    @login_required
    def published_stream(self, token: str, thread_id: UUID):
        body = self._stream_body()
        self.service.authorize(
            kind="web_app", scope=token, thread_id=thread_id, account=current_user,
        )
        events = self.service.stream(
            kind="web_app", scope=token, thread_id=thread_id, body=body,
        )
        return self._sse(events)

    @login_required
    def published_state(self, token: str, thread_id: UUID):
        self.service.authorize(
            kind="web_app", scope=token, thread_id=thread_id, account=current_user,
        )
        return jsonify(self.service.state(kind="web_app", scope=token, thread_id=thread_id))

    @login_required
    def published_messages(self, token: str, thread_id: UUID):
        self.service.authorize(
            kind="web_app", scope=token, thread_id=thread_id, account=current_user,
        )
        return jsonify(self._message_page(thread_id))

    def _message_page(self, thread_id: UUID) -> dict[str, Any]:
        before_value = request.args.get("before", "").strip()
        page_size = request.args.get("page_size", type=int) or 20
        return self.service.message_page(
            thread_id=thread_id,
            before=UUID(before_value) if before_value else None,
            page_size=page_size,
        )

    @login_required
    def cancel_run(self, thread_id: UUID, run_id: UUID):
        self.service.cancel(
            thread_id=thread_id,
            run_id=run_id,
            account=current_user,
        )
        return ("", 204)

    @staticmethod
    def _sse(events):
        @stream_with_context
        def generate():
            # 立即提交响应头，避免浏览器或反向代理等待首个业务事件。
            yield ": connected\n\n"
            for event in events:
                if event is None:
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=True)}\n\n"

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )


__all__ = ["AgentWorkbenchHandler"]
