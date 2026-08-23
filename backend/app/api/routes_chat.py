"""Chat API: natural-language business questions.

``POST /api/chat`` is the agent's only public entrance. The caller is identified
by the ``X-User`` header, their role and data scope are loaded from the database,
and every query the agent runs is filtered by that scope.

Nothing internal is exposed: no SQL, no view definitions, no system prompt, no
credentials. Errors carry a user-safe message and a stable error code.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..ai.agent import BusinessIntelligenceAgent
from ..ai.llm import build_llm_client
from ..ai.permission_filter import UserContext
from ..ai.schemas import ChatRequest, ChatResponse
from ..ai.tools import REGISTRY
from ..auth import audit
from ..database.models_ai import AuditAction, ChatConversation
from ..auth.permissions import require_section
from ..security.sections import SectionKey
from .deps import get_session, internal_error

logger = logging.getLogger("app.api.chat")

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.AI_ASSISTANT)),
) -> ChatResponse:
    """Ask a business question in English, Bangla or a mix of both."""
    agent = BusinessIntelligenceAgent(session, user, llm=build_llm_client())
    try:
        response = agent.chat(request.message, request.conversation_id)
        # Audited like any other report access: who asked what, and which tools
        # ran. The question is business text; no credential can reach here.
        audit.record(
            session, action=AuditAction.AI_QUERY, user_id=user.user_id,
            username=user.username, resource="chat",
            ip_address=audit.client_ip(http_request),
            detail={
                "question": request.message,
                "intent": response.intent.value,
                "tools": response.tools_used,
                "conversation_id": response.conversation_id,
                "elapsed_ms": response.elapsed_ms,
            },
            success=response.error_code is None,
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - internals never reach the caller
        session.rollback()
        raise internal_error(exc, "chat") from exc
    return response


@router.get("/chat/conversations")
def list_conversations(
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.AI_ASSISTANT)),
) -> dict[str, Any]:
    """The caller's own conversations, most recent first."""
    rows = session.execute(
        select(ChatConversation)
        .where(ChatConversation.user_id == user.user_id)
        .order_by(desc(ChatConversation.started_at))
        .limit(limit)
    ).scalars().all()
    return {
        "conversations": [
            {
                "conversation_id": row.conversation_id,
                "title": row.title,
                "started_at": row.started_at,
                "last_message_at": row.last_message_at,
                "message_count": row.message_count,
            }
            for row in rows
        ]
    }


@router.get("/chat/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.AI_ASSISTANT)),
) -> dict[str, Any]:
    """Message history for one of the caller's conversations."""
    agent = BusinessIntelligenceAgent(session, user)
    messages = agent.conversation_history(conversation_id)
    if not messages:
        # A conversation belonging to someone else is indistinguishable from one
        # that does not exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    return {"conversation_id": conversation_id, "messages": messages}


@router.get("/chat/capabilities")
def capabilities(user: UserContext = Depends(require_section(SectionKey.AI_ASSISTANT))) -> dict[str, Any]:
    """What the agent can answer, and the caller's own access scope."""
    return {
        "user": {
            "username": user.username,
            "role": user.role,
            "data_scope": user.data_scope,
            "scope_description": user.describe_scope(),
        },
        "languages": ["English", "Bangla", "Mixed Bangla-English"],
        "tools": [
            {"name": name, "description": spec.description,
             "intents": [i.value for i in spec.intents]}
            for name, spec in sorted(REGISTRY.items())
        ],
        "examples": [
            "আজকের sales কত?",
            "এই মাসের sales দেখাও",
            "Dhaka region-এর sales কত?",
            "গত মাসের তুলনায় sales কত বেড়েছে?",
            "Top 15 brands দেখাও",
            "Material group wise stock দেখাও",
            "কোন material-এর stock expire হবে?",
            "Region-wise target achievement দেখাও",
            "Which territory is underperforming?",
            "Why is sales down?",
            "Give me today's management summary",
        ],
    }
