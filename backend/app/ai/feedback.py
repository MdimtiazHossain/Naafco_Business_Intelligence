"""Recording what a reader thought of an answer.

The first half of the learning subsystem, and the half everything else depends
on: mining ``chat_messages`` finds the questions the agent *knew* it had
mishandled, but an answer that was confidently wrong looks identical to one that
was right. Only a reader can tell those apart, so this is where that judgement
enters the system.

Three things are enforced here rather than left to the caller:

* **A reader may only judge their own conversation.** The message is loaded and
  its conversation's owner checked before anything is written. A message id is a
  guessable integer, so without this check one user could attach text to
  another's conversation.
* **One verdict per reader per answer, changeable.** The table's unique
  constraint says at most one, and this module updates rather than inserting a
  second, so changing one's mind is a correction and not a second vote.
* **The free text is sanitised on the way in.** It is the only user-authored
  prose these tables hold, and an approved example built from it may later be
  shown to an administrator or handed to a language model as a worked example.
  The strings are cleaned by ``prompts.sanitize_message`` and whatever
  ``detect_injection`` matched is stored beside the text, so a reviewer sees the
  warning at the same moment they see the words.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models_ai import ChatConversation, ChatMessage
from ..database.models_learning import AgentFeedback, FeedbackRating
from . import mining
from .exceptions import AgentError
from .permission_filter import UserContext
from .prompts import sanitize_message
from .schemas import FeedbackRequest, FeedbackResponse

logger = logging.getLogger("app.ai.feedback")


class FeedbackRefused(AgentError):
    """The message cannot be judged by this caller."""

    code = "FEEDBACK_REFUSED"
    user_message = (
        "That answer can't be rated — it isn't part of one of your conversations."
    )


def _load_own_message(session: Session, user: UserContext,
                      message_id: int) -> ChatMessage:
    """The assistant turn, if it belongs to a conversation this caller owns.

    The same refusal for "no such message" and "not yours", deliberately: a
    distinct not-found would let a caller enumerate which message ids exist in
    other people's conversations.
    """
    message = session.get(ChatMessage, message_id)
    if message is not None and message.role == "assistant":
        conversation = session.get(ChatConversation, message.conversation_id)
        if conversation is not None and conversation.user_id == user.user_id:
            return message

    logger.warning("feedback refused for message=%s user=%s", message_id,
                   user.user_id)
    raise FeedbackRefused(
        f"message {message_id} is not an assistant turn owned by "
        f"user {user.user_id}"
    )


def _question_behind(session: Session, answer: ChatMessage) -> str | None:
    """The user's question that this answer replied to.

    A turn is stored as a user row immediately followed by an assistant row, so
    the question is the highest-numbered user message below the answer. It is
    the question, not the answer, that a reviewer can act on.
    """
    return session.execute(
        select(ChatMessage.message).where(
            ChatMessage.conversation_id == answer.conversation_id,
            ChatMessage.role == "user",
            ChatMessage.message_id < answer.message_id,
        ).order_by(ChatMessage.message_id.desc()).limit(1)
    ).scalar_one_or_none()


def record(session: Session, user: UserContext,
           request: FeedbackRequest) -> FeedbackResponse:
    """Store one reader's verdict, replacing their previous one if any."""
    message = _load_own_message(session, user, request.message_id)

    expected: str | None = None
    flags: list[str] = []
    if request.expected and request.expected.strip():
        cleaned, flags = sanitize_message(request.expected.strip())
        # A note that was *entirely* injection leaves nothing behind. The
        # verdict still counts — the reader did say the answer was wrong — but
        # there is no text to keep, and storing an empty string would look like
        # they typed nothing.
        expected = cleaned or None
        if flags:
            logger.warning(
                "injection patterns in feedback text: message=%s user=%s count=%s",
                message.message_id, user.user_id, len(flags),
            )

    existing = session.execute(
        select(AgentFeedback).where(
            AgentFeedback.message_id == message.message_id,
            AgentFeedback.user_id == user.user_id,
        )
    ).scalar_one_or_none()

    # A rejection is a signal; approval is not. Counted only when the verdict
    # *becomes* negative, so a reader toggling their thumb back and forth does
    # not inflate the count of a question that failed once.
    if request.rating == FeedbackRating.DOWN and (
        existing is None or existing.rating != FeedbackRating.DOWN
    ):
        mining.mine_negative_feedback(
            session, _question_behind(session, message), language=message.language,
        )

    if existing is not None:
        existing.rating = request.rating
        existing.expected = expected
        existing.injection_flags = {"patterns": flags} if flags else None
        existing.created_at = dt.datetime.now(dt.timezone.utc)
        session.flush()
        return FeedbackResponse(
            message_id=message.message_id, rating=existing.rating,
            updated=True, sanitized=bool(flags),
        )

    session.add(AgentFeedback(
        message_id=message.message_id,
        conversation_id=message.conversation_id,
        user_id=user.user_id,
        rating=request.rating,
        expected=expected,
        injection_flags={"patterns": flags} if flags else None,
    ))
    session.flush()
    return FeedbackResponse(
        message_id=message.message_id, rating=request.rating,
        updated=False, sanitized=bool(flags),
    )


def for_messages(session: Session, user: UserContext,
                 message_ids: list[int]) -> dict[int, str]:
    """This reader's own verdicts on the given messages, for replaying history.

    Only their own: a rating is one person's opinion, and showing a thumb the
    reader did not press would misreport it as theirs.
    """
    if not message_ids:
        return {}
    rows = session.execute(
        select(AgentFeedback.message_id, AgentFeedback.rating).where(
            AgentFeedback.message_id.in_(message_ids),
            AgentFeedback.user_id == user.user_id,
        )
    ).all()
    return {message_id: rating for message_id, rating in rows}


__all__ = ["FeedbackRefused", "FeedbackRating", "for_messages", "record"]
