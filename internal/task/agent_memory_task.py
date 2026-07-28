"""Durable background extraction of structured agent memory facts."""

from __future__ import annotations

import logging
from uuid import UUID

from celery import shared_task

from config.config import Config


logger = logging.getLogger(__name__)


@shared_task(
    ignore_result=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def extract_agent_memory_facts(
    *,
    account_id: str,
    app_id: str,
    end_user_id: str | None,
    conversation_id: str,
    message_id: str,
    query: str,
    answer: str,
) -> int:
    """Reload the app model configuration and extract facts outside SSE.

    Only identifiers and plain text cross the Celery boundary. Language-model
    clients and SQLAlchemy sessions are created inside the worker process.
    """
    from app.http.module import injector
    from internal.model import App
    from internal.service import (
        AgentRuntimeService,
        AppConfigService,
        LanguageModelService,
    )
    from pkg.sqlalchemy import SQLAlchemy

    db = injector.get(SQLAlchemy)
    try:
        account_uuid = UUID(account_id)
        app_uuid = UUID(app_id)
        app = db.session.get(App, app_uuid)
        if app is None or app.account_id != account_uuid:
            logger.warning("Skip memory extraction for missing or foreign app %s", app_id)
            return 0
        app_config = injector.get(AppConfigService).get_app_config(app)
        llm = injector.get(LanguageModelService).load_language_model(
            app_config.get("model_config", {}),
            account_id=account_uuid,
        )
        return injector.get(AgentRuntimeService).extract_and_store_facts(
            account_id=account_uuid,
            app_id=app_uuid,
            end_user_id=UUID(end_user_id) if end_user_id else None,
            conversation_id=UUID(conversation_id),
            message_id=UUID(message_id),
            query=query,
            answer=answer,
            llm=llm,
        )
    finally:
        db.session.remove()


def schedule_agent_memory_extraction(
    *,
    account_id: UUID,
    app_id: UUID,
    end_user_id: UUID | None,
    conversation_id: UUID,
    message_id: UUID,
    query: str,
    answer: str,
) -> bool:
    """Enqueue extraction without delaying or failing the completed chat."""
    if (
        not Config().AGENT_AUTO_EXTRACT_MEMORY
        or not query.strip()
        or not answer.strip()
    ):
        return False
    try:
        extract_agent_memory_facts.delay(
            account_id=str(account_id),
            app_id=str(app_id),
            end_user_id=str(end_user_id) if end_user_id else None,
            conversation_id=str(conversation_id),
            message_id=str(message_id),
            query=query,
            answer=answer,
        )
    except Exception:
        logger.exception(
            "Unable to enqueue memory extraction for message %s",
            message_id,
        )
        return False
    return True


__all__ = [
    "extract_agent_memory_facts",
    "schedule_agent_memory_extraction",
]
