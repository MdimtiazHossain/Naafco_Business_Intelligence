"""The agent facade: conversation memory, observability and the public entry point.

``BusinessIntelligenceAgent.chat`` is what the API calls. It loads the
conversation (and only ever the caller's own), runs the orchestrator, persists
the turn and the tool calls, and returns a :class:`ChatResponse`.

Logged per turn: conversation, user, intent, tool, execution time, success or
failure and the error code. Never logged: the system prompt, API keys, database
credentials, SQL, or raw row data.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..database.models_ai import ChatConversation, ChatMessage, ChatToolCall
from . import mining
from .exceptions import AgentError
from .llm import LLMClient, build_llm_client
from .orchestrator import AgentAnswer, ConversationContext, Orchestrator
from .permission_filter import UserContext
from .schemas import ChatResponse, Intent

logger = logging.getLogger("app.ai.agent")

#: How many previous turns are replayed to the model.
HISTORY_TURNS = 8


@dataclass
class ConversationState:
    conversation: ChatConversation
    history: list[dict[str, str]]
    context: ConversationContext


class BusinessIntelligenceAgent:
    """Public entry point for the chat API."""

    def __init__(self, session: Session, user: UserContext,
                 llm: LLMClient | None = None, today: dt.date | None = None) -> None:
        self.session = session
        self.user = user
        self.llm = llm if llm is not None else build_llm_client()
        self.today = today or dt.date.today()

    # -- conversation -------------------------------------------------------

    def _load_conversation(self, conversation_id: str | None) -> ConversationState:
        """Load the caller's conversation, or start a new one.

        A conversation belonging to another user is never loaded — an unknown or
        foreign id simply starts a fresh thread rather than leaking context.
        """
        conversation: ChatConversation | None = None
        if conversation_id:
            conversation = self.session.get(ChatConversation, conversation_id)
            if conversation is not None and conversation.user_id != self.user.user_id:
                logger.warning(
                    "conversation %s requested by a different user; starting a new one",
                    conversation_id,
                )
                conversation = None

        if conversation is None:
            conversation = ChatConversation(
                conversation_id=str(uuid.uuid4()),
                user_id=self.user.user_id,
                message_count=0,
                context=None,
            )
            self.session.add(conversation)
            self.session.flush()
            return ConversationState(conversation, [], ConversationContext())

        rows = self.session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation.conversation_id)
            .order_by(desc(ChatMessage.message_id))
            .limit(HISTORY_TURNS * 2)
        ).scalars().all()
        history = [
            {"role": row.role, "content": row.message} for row in reversed(rows)
        ]
        return ConversationState(
            conversation, history, ConversationContext.from_dict(conversation.context)
        )

    def _persist(self, state: ConversationState, message: str,
                 answer: AgentAnswer) -> int:
        """Store the turn, the assistant reply and every tool call."""
        conversation = state.conversation

        user_row = ChatMessage(
            conversation_id=conversation.conversation_id,
            user_id=self.user.user_id,
            role="user",
            message=message,
            language=answer.language,
            intent=answer.intent.value if answer.intent else None,
        )
        self.session.add(user_row)
        self.session.flush()

        assistant_row = ChatMessage(
            conversation_id=conversation.conversation_id,
            user_id=self.user.user_id,
            role="assistant",
            message=answer.answer,
            language=answer.language,
            intent=answer.intent.value if answer.intent else None,
            structured_query=(
                answer.query.model_dump(mode="json") if answer.query else None
            ),
            error_code=answer.error_code,
            elapsed_ms=answer.elapsed_ms,
        )
        self.session.add(assistant_row)
        self.session.flush()

        for invocation in answer.invocations:
            self.session.add(ChatToolCall(
                conversation_id=conversation.conversation_id,
                message_id=assistant_row.message_id,
                user_id=self.user.user_id,
                tool_name=invocation.tool_name,
                arguments=invocation.arguments,
                success=invocation.success,
                error_code=invocation.error_code,
                row_count=invocation.result.row_count if invocation.result else None,
                execution_ms=invocation.execution_ms,
            ))

        conversation.message_count = (conversation.message_count or 0) + 2
        conversation.last_message_at = dt.datetime.now(dt.timezone.utc)
        if answer.query is not None:
            conversation.context = answer.context.to_dict()
        if not conversation.title:
            conversation.title = message[:120]
        self.session.flush()

        # Whatever this turn has to teach, counted into the review queue. Each
        # write runs in its own SAVEPOINT inside ``mining.record``; this guard
        # covers the rest of the call, so that no failure in an analytics
        # side-effect — not a broken table, not a bug in the mining rules — can
        # cost a user the answer they asked for.
        try:
            mining.mine_turn(self.session, message, answer)
        except Exception:  # noqa: BLE001 - mining never breaks a conversation
            logger.exception("signal mining failed for conversation %s",
                             conversation.conversation_id)

        return assistant_row.message_id

    # -- public API ---------------------------------------------------------

    def chat(self, message: str, conversation_id: str | None = None,
             page: ChatContext | None = None) -> ChatResponse:
        """Answer one question in the context of a conversation."""
        state = self._load_conversation(conversation_id)
        orchestrator = Orchestrator(self.session, self.user, self.llm, self.today)

        try:
            answer = orchestrator.answer(message, state.context, state.history, page)
        except AgentError as exc:
            answer = AgentAnswer(answer=exc.user_message, intent=Intent.UNKNOWN,
                                 error_code=exc.code, error_details=exc.details)
        except Exception as exc:  # noqa: BLE001 - never surface internals
            logger.exception("agent failed for conversation %s",
                             state.conversation.conversation_id)
            answer = AgentAnswer(
                answer="I couldn't generate the report right now. Please try again.",
                intent=Intent.UNKNOWN, error_code="AGENT_ERROR",
            )

        message_id = self._persist(state, message, answer)

        logger.info(
            "chat conversation=%s user=%s intent=%s tools=%s elapsed_ms=%s success=%s "
            "error=%s",
            state.conversation.conversation_id, self.user.user_id,
            answer.intent.value if answer.intent else None,
            ",".join(answer.tools_used) or "-", answer.elapsed_ms,
            answer.error_code is None, answer.error_code or "-",
        )
        if answer.injection_detected:
            logger.warning(
                "prompt-injection patterns detected and neutralised: conversation=%s "
                "user=%s", state.conversation.conversation_id, self.user.user_id,
            )

        return ChatResponse(
            conversation_id=state.conversation.conversation_id,
            message_id=message_id,
            intent=answer.intent,
            answer=answer.answer,
            data=self._payload(answer),
            filters=answer.results[0].filters if answer.results else {},
            date_range=(
                answer.query.date_range.model_dump(mode="json")
                if answer.query and answer.query.date_range else {}
            ),
            sources=answer.sources,
            assumptions=answer.query.assumptions if answer.query else [],
            chart=answer.chart,
            needs_clarification=answer.needs_clarification,
            error_code=answer.error_code,
            language=answer.language,
            tools_used=answer.tools_used,
            elapsed_ms=answer.elapsed_ms,
        ) if True else None  # noqa: SIM210 - kept explicit for readability

    @staticmethod
    def _payload(answer: AgentAnswer) -> dict[str, Any]:
        """Machine-readable data behind the answer, ready for export or a chart."""
        if not answer.results:
            return {}
        primary = answer.results[0]
        return {
            "tool": primary.tool,
            "metric": primary.metric,
            "currency": primary.currency,
            "value": primary.value,
            "values": primary.values,
            "rows": primary.rows,
            "row_count": primary.row_count,
            "truncated": primary.truncated,
            "facts": primary.facts,
            "interpretations": primary.interpretations,
            "notes": primary.notes,
            "companions": [
                {"tool": r.tool, "value": r.value, "values": r.values}
                for r in answer.results[1:]
            ],
        }

    def conversation_history(self, conversation_id: str,
                             limit: int = 50) -> list[dict[str, Any]]:
        """Past turns of the caller's own conversation."""
        conversation = self.session.get(ChatConversation, conversation_id)
        if conversation is None or conversation.user_id != self.user.user_id:
            return []
        rows = self.session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.message_id)
            .limit(limit)
        ).scalars().all()
        return [
            {
                "message_id": row.message_id,
                "role": row.role,
                "message": row.message,
                "intent": row.intent,
                "language": row.language,
                "created_at": row.created_at,
                "elapsed_ms": row.elapsed_ms,
            }
            for row in rows
        ]


__all__ = ["BusinessIntelligenceAgent", "ConversationState", "HISTORY_TURNS"]
