"""Turning what went wrong into a reviewable queue.

Every failure this module records was already visible somewhere — an
``UNKNOWN`` intent on ``chat_messages``, an error code, a tool call that
returned nothing, a thumbs-down. What was missing was somewhere to *accumulate*
them: a reviewer's first question is "what fails most often", and a hundred
separate rows saying the same thing does not answer it. So a signal is
identified by (kind of failure, normalised phrase) and **counted**, never
appended.

Nothing here proposes anything, and nothing here changes how the agent behaves.
It only writes down what happened, so that a person opening the review screen in
a later step has something to read.

**Which phrase is recorded matters more than it looks.** For a question the
agent could not classify, the phrase is the question. For an entity it could not
resolve, the phrase is the *term* — "chini", not "chinir sales koto" — because
the term is the thing somebody can teach and the sentence around it is noise
that would split one problem across a dozen rows. Carrying that term this far is
why ``AgentAnswer.error_details`` exists.

**Mining never breaks a conversation.** It runs inside a SAVEPOINT: if a signal
cannot be written, the savepoint rolls back and the turn it was mined from is
still saved and still answered. An analytics side-effect must not be able to
cost a user their answer.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database.models_ai import ChatMessage, ChatToolCall
from ..database.models_learning import AgentLearningSignal, SignalStatus, SignalType
from ..utils.text import normalize_text

logger = logging.getLogger("app.ai.mining")

#: The bound on ``agent_learning_signal.normalized_phrase``.
MAX_PHRASE = 300

#: Trailing punctuation that never distinguishes two questions.
#:
#: The Bangla danda (``।``) is here beside the Latin marks for the same reason:
#: "sales koto" and "sales koto?" are one problem, not two, and a reviewer
#: reading a queue split by punctuation would count the same failure twice.
_TRAILING = re.compile(r"[\s\.\?!,;:।]+$")


def normalize_phrase(text: str | None) -> str | None:
    """The dedup key for a phrase: trimmed, collapsed, lowercased, bounded.

    Lowercasing looks like a violation of this project's "no case folding" rule,
    and is deliberately not one. That rule protects *stored business data* — a
    code, a name, a Bangla string that must come back exactly as it arrived —
    and it is honoured here by keeping the untouched original in
    ``raw_sample``. This value is a grouping key and nothing else, so "Sales"
    and "sales" have to land on one row. Bangla is caseless, so ``lower()``
    leaves it byte-for-byte unchanged and only Latin text is affected at all.
    """
    cleaned = normalize_text(text)
    if cleaned is None:
        return None
    cleaned = _TRAILING.sub("", cleaned).lower()
    if not cleaned:
        return None
    return cleaned[:MAX_PHRASE]


def record(session: Session, signal_type: str, phrase: str | None, *,
           language: str | None = None, raw: str | None = None) -> bool:
    """Count one occurrence of one phrase failing one way.

    Increments an existing row or inserts the first. Returns whether anything
    was written, so a caller can log without inspecting the table.

    The whole write sits in a SAVEPOINT. Two callers racing on the same new
    phrase both pass the "does it exist" check and one loses on the unique
    constraint; that loser rolls back to the savepoint and increments instead,
    leaving the surrounding transaction untouched either way.
    """
    normalized = normalize_phrase(phrase)
    if normalized is None:
        return False

    now = dt.datetime.now(dt.timezone.utc)
    try:
        with session.begin_nested():
            existing = session.execute(
                select(AgentLearningSignal).where(
                    AgentLearningSignal.signal_type == signal_type,
                    AgentLearningSignal.normalized_phrase == normalized,
                )
            ).scalar_one_or_none()

            if existing is not None:
                existing.occurrences = (existing.occurrences or 0) + 1
                existing.last_seen_at = now
                return True

            session.add(AgentLearningSignal(
                signal_type=signal_type,
                normalized_phrase=normalized,
                # The untouched original, so a reviewer sees what was really
                # typed rather than the key it was reduced to.
                raw_sample=(raw or phrase or "")[:2000] or None,
                language=language,
                occurrences=1,
                status=SignalStatus.NEW,
                first_seen_at=now,
                last_seen_at=now,
            ))
            return True

    except IntegrityError:
        # Lost the race. The row exists now, so count against it.
        try:
            with session.begin_nested():
                session.execute(
                    AgentLearningSignal.__table__.update()
                    .where(
                        AgentLearningSignal.signal_type == signal_type,
                        AgentLearningSignal.normalized_phrase == normalized,
                    )
                    .values(
                        occurrences=AgentLearningSignal.occurrences + 1,
                        last_seen_at=now,
                    )
                )
            return True
        except Exception:  # noqa: BLE001 - analytics never costs an answer
            logger.warning("could not count signal %s after a race", signal_type)
            return False

    except Exception:  # noqa: BLE001 - analytics never costs an answer
        logger.exception("could not record signal %s", signal_type)
        return False


def _entity_term(error_details: dict[str, Any] | None) -> str | None:
    """The term an entity lookup failed on, if the failure named one."""
    if not error_details:
        return None
    term = error_details.get("term")
    return term if isinstance(term, str) and term.strip() else None


def signals_for_turn(message: str, *, intent: str | None, error_code: str | None,
                     error_details: dict[str, Any] | None,
                     empty_result: bool) -> list[tuple[str, str]]:
    """Which signals one completed turn produces, as ``(type, phrase)`` pairs.

    Pure, so the decisions are testable without a database. At most one signal
    per turn: a question that failed to resolve an entity also has an
    ``UNKNOWN`` intent, and recording both would double-count one failure and
    put the useless half — the whole sentence — in front of a reviewer beside
    the useful half.
    """
    if error_code == SignalType.ENTITY_NOT_FOUND:
        term = _entity_term(error_details)
        # Only the term is worth recording. Without one there is nothing here a
        # person could act on, so nothing is written.
        return [(SignalType.ENTITY_NOT_FOUND, term)] if term else []

    if error_code == SignalType.UNSUPPORTED:
        return [(SignalType.UNSUPPORTED, message)]

    if intent == "UNKNOWN":
        return [(SignalType.UNKNOWN_INTENT, message)]

    # Not a failure: the agent understood the question and the warehouse had
    # nothing to say. Recorded anyway, because a phrase that reliably comes back
    # empty is worth a person's eyes — and left as a separate kind so a reviewer
    # can tell "I don't understand" from "there is no data".
    if empty_result and not error_code:
        return [(SignalType.ZERO_ROWS, message)]

    return []


def result_is_empty(answer: Any) -> bool:
    """True when a successful answer carried no figure at all.

    A scalar answer — one KPI — has ``value`` set and no rows, and must not be
    mistaken for an empty one; without this check every single-number answer
    would file a ZERO_ROWS signal and bury the queue.
    """
    results = getattr(answer, "results", None) or []
    if not results:
        return False
    return all(
        result.value is None and not result.values and not result.rows
        for result in results
    )


def mine_turn(session: Session, message: str, answer: Any) -> int:
    """Record whatever one finished turn has to teach. Returns signals written."""
    pairs = signals_for_turn(
        message,
        intent=answer.intent.value if getattr(answer, "intent", None) else None,
        error_code=getattr(answer, "error_code", None),
        error_details=getattr(answer, "error_details", None),
        empty_result=result_is_empty(answer),
    )
    language = getattr(answer, "language", None)
    written = 0
    for signal_type, phrase in pairs:
        if record(session, signal_type, phrase, language=language, raw=phrase):
            written += 1
    return written


def mine_negative_feedback(session: Session, question: str | None, *,
                           language: str | None = None) -> bool:
    """Record that a reader rejected the answer to this question.

    The *question* is recorded rather than the answer: the answer is what went
    wrong, but the question is what has to be handled better next time, and it
    is the only half a reviewer can do anything with.
    """
    return record(session, SignalType.NEGATIVE_FEEDBACK, question,
                  language=language, raw=question)


def mine_history(session: Session, *, limit: int | None = None) -> dict[str, int]:
    """Sweep conversations already stored, for signals nobody was mining yet.

    The live hook only sees turns taken after it existed, which on the day this
    ships is none of them. This reads what ``chat_messages`` and
    ``chat_tool_calls`` already record and produces the same signals from it, so
    a reviewer opens a queue with history in it rather than an empty table.

    Not idempotent, and deliberately not disguised as such: running it twice
    counts every historical turn twice. It is an operator's one-off, and
    ``occurrences`` is a ranking signal rather than an audited figure, so a
    re-run distorts an ordering rather than corrupting a record.
    """
    statement = (
        select(ChatMessage)
        .where(ChatMessage.role == "assistant")
        .order_by(ChatMessage.message_id)
    )
    if limit:
        statement = statement.limit(limit)
    rows = session.execute(statement).scalars().all()

    # Which assistant turns produced a tool call that came back with no rows.
    # Read in one query rather than per message: a per-row lookup here would be
    # one statement per historical turn.
    empty_ids = {
        message_id
        for (message_id,) in session.execute(
            select(ChatToolCall.message_id)
            .where(ChatToolCall.success.is_(True))
            .group_by(ChatToolCall.message_id)
            .having(func.coalesce(func.sum(ChatToolCall.row_count), 0) == 0)
        ).all()
        if message_id is not None
    }

    questions = _questions_by_assistant_turn(session, rows)

    counts: dict[str, int] = {}
    for row in rows:
        question = questions.get(row.message_id)
        if not question:
            continue
        pairs = signals_for_turn(
            question,
            intent=row.intent,
            error_code=row.error_code,
            # A stored turn kept the error code but never the exception's
            # details, so a historical ENTITY_NOT_FOUND cannot name its term and
            # is skipped rather than filed against the whole sentence.
            error_details=None,
            empty_result=row.message_id in empty_ids,
        )
        for signal_type, phrase in pairs:
            if record(session, signal_type, phrase, language=row.language,
                      raw=phrase):
                counts[signal_type] = counts.get(signal_type, 0) + 1
    return counts


def _questions_by_assistant_turn(
    session: Session, assistant_rows: Sequence[ChatMessage],
) -> dict[int, str]:
    """The user question that preceded each assistant turn.

    A turn is stored as a user row immediately followed by an assistant row, so
    the question is the highest-numbered user message below the answer.
    """
    if not assistant_rows:
        return {}
    conversation_ids = {row.conversation_id for row in assistant_rows}
    user_rows = session.execute(
        select(ChatMessage)
        .where(ChatMessage.role == "user",
               ChatMessage.conversation_id.in_(conversation_ids))
        .order_by(ChatMessage.message_id)
    ).scalars().all()

    by_conversation: dict[str, list[ChatMessage]] = {}
    for row in user_rows:
        by_conversation.setdefault(row.conversation_id, []).append(row)

    questions: dict[int, str] = {}
    for row in assistant_rows:
        candidates = [
            candidate for candidate in by_conversation.get(row.conversation_id, [])
            if candidate.message_id < row.message_id
        ]
        if candidates:
            questions[row.message_id] = candidates[-1].message
    return questions


__all__ = [
    "MAX_PHRASE",
    "mine_history",
    "mine_negative_feedback",
    "mine_turn",
    "normalize_phrase",
    "record",
    "result_is_empty",
    "signals_for_turn",
]
