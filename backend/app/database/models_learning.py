"""Agent learning: what the agent got wrong, and the vocabulary it was taught.

The agent's resolution chain is deliberately deterministic — ``ai/intent.py``
matches hand-written keyword dictionaries, ``ai/entity_resolver.py`` matches
master-data names, and neither guesses. That is what makes an answer
reproducible, and it is also why the agent cannot understand a word nobody has
written down yet.

These four tables close that gap without giving anything up. They record where
the agent failed, and they hold the vocabulary and worked examples a human has
**approved**. Four rules bound the whole subsystem:

1. **Nothing learned ever touches a number.** These tables affect interpretation
   only — which tool runs, which entity a word names, which period is meant.
   Aggregation, filtering, scope and permission are untouched, so a learned
   alias can change *which* question gets answered and never *what the answer
   is*.
2. **An alias never outranks the master data.** It is consulted where the master
   has no answer, so the master stays the authority on its own records.
3. **Nothing activates on its own.** A row is proposed, a person approves it,
   and only then is it read. An agent that silently taught itself a wrong
   mapping would answer confidently and wrongly for every future question, which
   is strictly worse than answering "not found".
4. **Nothing is deleted.** A mapping that turns out to be wrong is retired and
   stays readable, like every other record in this system.

Free text written by a user reaches these tables — the "what did you expect?"
box on a piece of feedback, and the question behind a mined example. It is
sanitised through ``ai/prompts.sanitize_message`` before it is stored, and what
``detect_injection`` found is stored beside it, because an approved example is
eventually shown to an administrator and may be fed to a language model as a
worked example. That is a prompt-injection path, and it is closed here rather
than at the point of use.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import CODE, SURROGATE_PK, Base
from .models_warehouse import FK_TYPE, JSON_TYPE


class FeedbackRating:
    """A reader's verdict on one answer."""

    UP = "UP"
    DOWN = "DOWN"

    ALL = (UP, DOWN)


class SignalType:
    """The ways an answer can disappoint, each mined from what is already stored.

    The first three are recorded by the agent itself as an ``error_code`` or an
    ``UNKNOWN`` intent. ``ZERO_ROWS`` is not an error at all — a question can be
    understood perfectly and have no rows behind it — but a phrase that reliably
    returns nothing is worth a human's attention, so it is mined and left for
    triage rather than treated as a fault.
    """

    UNKNOWN_INTENT = "UNKNOWN_INTENT"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"
    ZERO_ROWS = "ZERO_ROWS"
    NEGATIVE_FEEDBACK = "NEGATIVE_FEEDBACK"

    ALL = (UNKNOWN_INTENT, ENTITY_NOT_FOUND, UNSUPPORTED, ZERO_ROWS,
           NEGATIVE_FEEDBACK)


class SignalStatus:
    """Where a mined signal sits in a reviewer's queue."""

    NEW = "NEW"
    TRIAGED = "TRIAGED"
    PROPOSED = "PROPOSED"
    DISMISSED = "DISMISSED"

    ALL = (NEW, TRIAGED, PROPOSED, DISMISSED)


class AliasKind:
    """What a learned phrase resolves to.

    ``ENTITY`` names a master record and is answered by ``entity_resolver``; the
    other three name a key that already exists in ``ai/intent.py``'s keyword
    dictionaries, so approving one widens the vocabulary of a classifier that is
    otherwise editable only by changing code.
    """

    ENTITY = "ENTITY"
    METRIC = "METRIC"
    GROUP_BY = "GROUP_BY"
    MODIFIER = "MODIFIER"

    ALL = (ENTITY, METRIC, GROUP_BY, MODIFIER)


class LearningStatus:
    """The approval lifecycle, shared by aliases and examples.

    ``RETIRED`` rather than deleted, and ``REJECTED`` kept rather than discarded:
    knowing that a mapping was considered and turned down is what stops it being
    proposed again every week.
    """

    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"

    ALL = (PROPOSED, ACTIVE, REJECTED, RETIRED)


class LearningSource:
    """Whether a row was mined from use or written by hand."""

    MINED = "MINED"
    MANUAL = "MANUAL"

    ALL = (MINED, MANUAL)


class AgentFeedback(Base):
    """One reader's verdict on one answer.

    The ground truth everything else is built on: without somebody saying an
    answer was wrong there is no signal to learn from, only traffic.
    """

    __tablename__ = "agent_feedback"

    feedback_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                             autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("chat_messages.message_id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[int] = mapped_column(FK_TYPE, nullable=False)
    rating: Mapped[str] = mapped_column(String(8), nullable=False)

    #: What the reader says they expected, sanitised on the way in.
    expected: Mapped[str | None] = mapped_column(Text)
    #: What ``detect_injection`` found in ``expected``, if anything. Stored so a
    #: reviewer sees the warning at the same moment they see the text.
    injection_flags: Mapped[dict | None] = mapped_column(JSON_TYPE)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # One verdict per person per answer. A reader may change their mind —
        # the row is updated — but two rows would let one loud user outvote the
        # rest by clicking twice.
        UniqueConstraint("message_id", "user_id",
                         name="uq_agent_feedback_message_user"),
        Index("ix_agent_feedback_message_id", "message_id"),
        Index("ix_agent_feedback_rating", "rating"),
        Index("ix_agent_feedback_created_at", "created_at"),
    )


class AgentLearningSignal(Base):
    """A phrase the agent handled badly, and how often.

    Deduplicated on arrival: the same question asked forty times is one row with
    ``occurrences`` at forty, not forty rows. A reviewer's first question is
    always "what fails most", and a table that answers it by counting is a table
    nobody has to aggregate.
    """

    __tablename__ = "agent_learning_signal"

    signal_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                           autoincrement=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)

    #: The phrase, normalised by ``utils.text.normalize_text`` and truncated.
    #:
    #: Bounded rather than ``Text`` because it carries a unique constraint, and
    #: PostgreSQL's btree refuses an index entry over about 2.7 kB. A mined
    #: phrase is a few words; the miner truncates, so the bound never silently
    #: merges two different phrases in practice.
    normalized_phrase: Mapped[str] = mapped_column(String(300), nullable=False)
    #: One representative original, kept verbatim so a reviewer sees what was
    #: actually typed rather than the normalised form.
    raw_sample: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8))

    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=SignalStatus.NEW)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("signal_type", "normalized_phrase",
                         name="uq_agent_learning_signal_type_phrase"),
        Index("ix_agent_learning_signal_status", "status"),
        Index("ix_agent_learning_signal_occurrences", "occurrences"),
        Index("ix_agent_learning_signal_last_seen", "last_seen_at"),
    )


class AgentTermAlias(Base):
    """A phrase a person has taught the agent, and what it means.

    Consulted by ``entity_resolver`` and ``intent`` **only when they have no
    answer of their own**, so the master data and the shipped keyword lists stay
    authoritative and an alias can only ever add reach, never redirect it.
    """

    __tablename__ = "agent_term_alias"

    alias_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                          autoincrement=True)
    phrase: Mapped[str] = mapped_column(String(200), nullable=False)
    language: Mapped[str | None] = mapped_column(String(8))
    alias_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: Set together, and only for ``AliasKind.ENTITY``.
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_code: Mapped[str | None] = mapped_column(CODE)
    #: Set for the other three kinds: the key in ``intent.py``'s dictionaries
    #: this phrase should count as.
    target_keyword: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=LearningStatus.PROPOSED)
    source: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=LearningSource.MINED)
    signal_id: Mapped[int | None] = mapped_column(
        FK_TYPE,
        ForeignKey("agent_learning_signal.signal_id", ondelete="SET NULL"),
    )

    #: Exactly ``kind|phrase`` while the row is ACTIVE, and NULL in every other
    #: state.
    #:
    #: This is how "only one active meaning per phrase" is enforced on both
    #: dialects at once. A partial unique index would say it directly but is
    #: PostgreSQL-only; a plain unique over ``(phrase, alias_kind)`` would say
    #: too much, blocking a retired mapping from ever being replaced by a
    #: better one. NULLs are distinct in a unique constraint on both SQLite and
    #: PostgreSQL, so any number of retired rows coexist while at most one
    #: active row can hold the key.
    #:
    #: ``language`` is deliberately *not* part of it. The resolver looks a
    #: phrase up by the phrase, so two active rows for one phrase in different
    #: languages would be an ambiguity it could not resolve — and refusing
    #: ambiguity rather than picking a side is the rule this whole package is
    #: built around. Language stays an attribute describing where the phrase
    #: came from.
    active_key: Mapped[str | None] = mapped_column(String(220))

    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("active_key", name="uq_agent_term_alias_active_key"),
        Index("ix_agent_term_alias_phrase", "phrase"),
        Index("ix_agent_term_alias_status", "status"),
        Index("ix_agent_term_alias_kind", "alias_kind"),
    )


class AgentExample(Base):
    """A question, and the tool call that answered it correctly.

    Two consumers, which is why the bank is worth keeping even with no language
    model configured. The deterministic planner matches an approved question
    directly and reuses its tool; a configured model receives the same rows as
    worked examples. Neither is trusted with the result — the tool still
    validates its own arguments and the permission filter still applies — so an
    example can only ever improve *which* tool is chosen.
    """

    __tablename__ = "agent_example"

    example_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                            autoincrement=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: Normalised and truncated, for the same reason as on the signal table.
    normalized_question: Mapped[str] = mapped_column(String(300), nullable=False)
    language: Mapped[str | None] = mapped_column(String(8))

    intent: Mapped[str | None] = mapped_column(String(48))
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The sanitised arguments, codes and dates only — the same shape
    #: ``chat_tool_calls`` already stores. No SQL and no prompt text.
    arguments: Mapped[dict | None] = mapped_column(JSON_TYPE)

    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=LearningStatus.PROPOSED)
    source: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=LearningSource.MINED)
    source_message_id: Mapped[int | None] = mapped_column(
        FK_TYPE, ForeignKey("chat_messages.message_id", ondelete="SET NULL"),
    )
    #: How often this example has been matched. A bank nobody hits is a bank to
    #: prune, and the count is the only way to know which rows those are.
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: ``normalized_question`` while ACTIVE, NULL otherwise — the same
    #: single-active-row constraint, and for the same reason, as on the alias
    #: table above.
    active_key: Mapped[str | None] = mapped_column(String(300))

    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("active_key", name="uq_agent_example_active_key"),
        Index("ix_agent_example_normalized", "normalized_question"),
        Index("ix_agent_example_status", "status"),
        Index("ix_agent_example_tool_name", "tool_name"),
    )


__all__ = [
    "AgentExample",
    "AgentFeedback",
    "AgentLearningSignal",
    "AgentTermAlias",
    "AliasKind",
    "FeedbackRating",
    "LearningSource",
    "LearningStatus",
    "SignalStatus",
    "SignalType",
]
