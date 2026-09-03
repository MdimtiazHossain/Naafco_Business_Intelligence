"""The five states a piece of data can be in, and why they are five.

A business user reading "0" cannot tell whether the business sold nothing, the
table is empty, the mapping is missing, the master was never configured, or the
question does not apply to this record. Those are five different situations
calling for five different actions — and collapsing them into one is how a
system quietly lies about what it knows.

``VALID_ZERO``
    A real measurement of nothing. The business did not sell this material in
    this month, and that is a fact worth planning around. **Renders as ``0``.**

``NO_DATA``
    The source is empty. No sales have been loaded at all, so there is nothing
    to measure — which is not the same as having measured nothing. **Renders as
    ``n/a``, with what to load.**

``INSUFFICIENT_DATA``
    The source has rows, but not the ones this calculation needs. The Customer
    Master is full and states no sub-territory, so a customer-level allocation
    cannot be reached. **Renders as a blocked state naming the gap.**

``INVALID_DATA``
    The data is present and contradicts itself — a territory whose unit does not
    exist, a sub-territory pointing at a retired territory. Not missing;
    **wrong**, and wrong in a way that would corrupt a result computed from it.

``NOT_AVAILABLE``
    The platform holds no source for this at all. Customer Potential has no
    master, discontinued-SKU status has no column. Distinct from ``NO_DATA``
    because loading a file will not fix it — somebody has to introduce the
    master first.

``NOT_APPLICABLE``
    The question does not apply to this record. A discontinued material has no
    forward target; a sub-territory with no customers has no customer split.
    Nothing is missing and nothing is wrong.

Every readiness check, factor availability report and allocation warning in this
package states one of these rather than a bare boolean, so the screen can say
which of the five it is rather than making the reader guess from a dash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALID_ZERO = "VALID_ZERO"
NO_DATA = "NO_DATA"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
INVALID_DATA = "INVALID_DATA"
NOT_AVAILABLE = "NOT_AVAILABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"
#: Complete, consistent, and too big to run.
#:
#: The other states all describe *the data*. This one describes **the request**:
#: the projection succeeded and is exact, nothing is missing or contradictory,
#: and the plan as configured would write more rows than the ceiling allows.
#: It exists because the alternative was reporting a capacity refusal as
#: ``INSUFFICIENT_DATA``, which sent a reader looking for absent data when the
#: real answer was "narrow the plan" — the precise confusion this vocabulary was
#: introduced to prevent.
EXCEEDS_LIMIT = "EXCEEDS_LIMIT"
#: Everything needed is present and consistent.
AVAILABLE = "AVAILABLE"

ALL = (AVAILABLE, VALID_ZERO, NO_DATA, INSUFFICIENT_DATA, INVALID_DATA,
       EXCEEDS_LIMIT,
       NOT_AVAILABLE, NOT_APPLICABLE)

#: States that stop a calculation from producing a trustworthy answer.
#: ``VALID_ZERO`` is deliberately absent: zero is an answer.
BLOCKING = (NO_DATA, INSUFFICIENT_DATA, INVALID_DATA, EXCEEDS_LIMIT)

#: How each state reads as a signal. Colour is never the only cue — every
#: check carries its own sentence — but a reader scanning a list of twelve
#: needs to see at a glance which ones need them.
TONE: dict[str, str] = {
    AVAILABLE: "ok",
    VALID_ZERO: "ok",
    NO_DATA: "blocked",
    INSUFFICIENT_DATA: "blocked",
    INVALID_DATA: "error",
    # Red rather than amber: this is a refusal, not something to look into.
    EXCEEDS_LIMIT: "error",
    NOT_AVAILABLE: "muted",
    NOT_APPLICABLE: "muted",
}


@dataclass(frozen=True)
class DataState:
    """One check's verdict, in the vocabulary above.

    ``detail`` is the sentence a business user reads; ``action`` is what they
    would do about it. Both are written here rather than in the browser so the
    same words reach the screen, an export and a chat answer.
    """

    state: str
    detail: str
    action: str | None = None
    #: Counts behind the verdict — "0 of 2,091 mapped" is what makes a
    #: readiness line actionable rather than merely alarming.
    facts: dict[str, Any] | None = None
    #: Whether this particular check stops the run, independently of what the
    #: data state is.
    #:
    #: The two are genuinely different questions and conflating them was a bug.
    #: A missing Conversion Factor is honestly ``NO_DATA`` — the master states
    #: nothing — and it does **not** stop an allocation, because the engine
    #: allocates volume and volume needs neither derivation input. Blocking on
    #: it would make an unpriced material unallocatable, which is far worse than
    #: an ``n/a`` in two columns.
    #:
    #: ``None`` means "take the state's word for it", which is right for most
    #: checks; an advisory check passes ``False`` explicitly.
    blocks: bool | None = None

    @property
    def ok(self) -> bool:
        return self.state in (AVAILABLE, VALID_ZERO)

    @property
    def blocking(self) -> bool:
        if self.blocks is not None:
            return self.blocks
        return self.state in BLOCKING

    @property
    def tone(self) -> str:
        return TONE.get(self.state, "muted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "tone": self.tone,
            "ok": self.ok,
            "blocking": self.blocking,
            "detail": self.detail,
            "action": self.action,
            "facts": self.facts or {},
            "advisory": self.blocks is False,
        }


def available(detail: str, **facts: Any) -> DataState:
    return DataState(AVAILABLE, detail, facts=facts or None)


def exceeds_limit(detail: str, action: str, *, blocks: bool | None = None,
                  **facts: Any) -> DataState:
    """The data is fine; the run this plan asks for is too large.

    Kept apart from :func:`insufficient` deliberately. "Something is missing"
    and "this is complete and too big" send a reader to entirely different
    places, and only one of them is fixed by loading data.
    """
    return DataState(EXCEEDS_LIMIT, detail, action, facts or None, blocks)


def no_data(detail: str, action: str, *, blocks: bool | None = None,
            **facts: Any) -> DataState:
    return DataState(NO_DATA, detail, action, facts or None, blocks)


def insufficient(detail: str, action: str, *, blocks: bool | None = None,
                 **facts: Any) -> DataState:
    return DataState(INSUFFICIENT_DATA, detail, action, facts or None, blocks)


def invalid(detail: str, action: str, **facts: Any) -> DataState:
    return DataState(INVALID_DATA, detail, action, facts or None)


def not_available(detail: str, action: str | None = None, **facts: Any) -> DataState:
    return DataState(NOT_AVAILABLE, detail, action, facts or None)


def not_applicable(detail: str, **facts: Any) -> DataState:
    return DataState(NOT_APPLICABLE, detail, facts=facts or None)


__all__ = [
    "AVAILABLE",
    "VALID_ZERO",
    "NO_DATA",
    "INSUFFICIENT_DATA",
    "INVALID_DATA",
    "NOT_AVAILABLE",
    "NOT_APPLICABLE",
    "ALL",
    "BLOCKING",
    "TONE",
    "DataState",
    "available",
    "no_data",
    "insufficient",
    "invalid",
    "not_available",
    "not_applicable",
]
