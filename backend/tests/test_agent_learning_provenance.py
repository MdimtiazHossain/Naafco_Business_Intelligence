"""Regression tests for the two foreign keys the learning proposals never checked.

Both are client-supplied provenance ids. With SQLite's foreign keys off they
were stored dangling; with them on the flush raised, and the endpoint's generic
handler turned that into a 500. Either way the caller was told nothing useful,
so both are now refused by name.
"""

from __future__ import annotations

import pytest

from app.ai import vocabulary
from app.database.models_learning import AliasKind


def test_proposing_an_alias_from_a_signal_that_does_not_exist_is_refused(
    session, users,
) -> None:
    with pytest.raises(vocabulary.ReviewRefused) as raised:
        vocabulary.propose_alias(
            session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
            target_keyword="sales", signal_id=424242,
        )
    # The id is named, so a caller can see what was wrong with their request.
    assert "424242" in raised.value.user_message


def test_proposing_an_example_from_a_message_that_does_not_exist_is_refused(
    session, users,
) -> None:
    with pytest.raises(vocabulary.ReviewRefused) as raised:
        vocabulary.propose_example(
            session, users["ceo"], question="aajker sales koto",
            tool_name="get_sales_summary", source_message_id=999999,
        )
    assert "999999" in raised.value.user_message


def test_a_proposal_with_no_provenance_is_still_accepted(session, users) -> None:
    """The check must not make the optional field mandatory."""
    alias = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    assert alias["signal_id"] is None

    example = vocabulary.propose_example(
        session, users["ceo"], question="aajker sales koto",
        tool_name="get_sales_summary",
    )
    assert example["example_id"] > 0
