"""The approved example bank: what it may decide, and what it must never carry.

Two consumers, one bank. Without a language model an approved example is a
deterministic shortcut; with one it is also a worked example in the planner
prompt. Neither is trusted with a figure — the tool still validates its own
arguments and the permission filter still applies — so the whole surface an
example can move is *which report answers a question*.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import lexicon, vocabulary
from app.ai.lexicon import LearnedExample, Lexicon
from app.ai.prompts import MAX_PLANNER_EXAMPLES, planner_examples
from app.api.deps import get_session
from app.database.models_learning import AgentExample, LearningStatus
from app.main import app

pytest.importorskip("multipart",
                    reason="python-multipart is required by FastAPI forms")


@pytest.fixture(autouse=True)
def clean_cache():
    lexicon.invalidate_cache()
    yield
    lexicon.invalidate_cache()


@pytest.fixture
def client(agent_engine, users, monkeypatch):
    import app.ai.agent as agent_module
    from conftest_phase3 import TODAY

    original_init = agent_module.BusinessIntelligenceAgent.__init__

    def pinned_init(self, session, user, llm=None, today=None):
        original_init(self, session, user, llm=llm, today=today or TODAY)

    monkeypatch.setattr(agent_module.BusinessIntelligenceAgent, "__init__",
                        pinned_init)

    def _session_override():
        db = Session(bind=agent_engine, expire_on_commit=False, future=True)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = _session_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def approve(session, users, question: str, tool: str,
            intent: str | None = None) -> int:
    row = vocabulary.propose_example(
        session, users["ceo"], question=question, tool_name=tool, intent=intent,
    )
    vocabulary.approve_example(session, users["ceo"], row["example_id"])
    session.commit()
    lexicon.invalidate_cache()
    return row["example_id"]


# --------------------------------------------------------------------------
# The few-shot block
# --------------------------------------------------------------------------


def test_the_planner_block_names_the_tool_for_each_question() -> None:
    block = planner_examples([
        LearnedExample(1, "get_sales_summary", "SALES_SUMMARY", "aajker sales koto"),
    ])
    assert "aajker sales koto" in block
    assert "get_sales_summary" in block


def test_the_planner_block_is_empty_when_nothing_is_approved() -> None:
    """No approved examples must add nothing at all to the prompt."""
    assert planner_examples([]) == ""


def test_the_planner_block_is_bounded() -> None:
    """A bank that grows must not grow the prompt without limit."""
    many = [
        LearnedExample(i, "get_sales_summary", "SALES_SUMMARY", f"question {i}")
        for i in range(MAX_PLANNER_EXAMPLES * 3)
    ]
    lines = [line for line in planner_examples(many).splitlines()
             if line.startswith("- ")]
    assert len(lines) == MAX_PLANNER_EXAMPLES


def test_the_planner_block_does_not_carry_stored_arguments() -> None:
    """Those hold one past caller's dates and filters.

    A model shown them copies them instead of reading the question in front of
    it, which would answer a new question with an old scope.
    """
    example = LearnedExample(1, "get_sales_summary", "SALES_SUMMARY",
                             "aajker sales koto")
    block = planner_examples([example])
    assert "date_from" not in block
    assert "filters" not in block


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_only_approved_examples_are_loaded(session, users) -> None:
    vocabulary.propose_example(
        session, users["ceo"], question="aajker sales koto",
        tool_name="get_sales_summary",
    )
    session.flush()
    lexicon.invalidate_cache()
    assert lexicon.load(session).examples == {}


def test_approving_puts_an_example_in_the_bank(session, users) -> None:
    approve(session, users, "aajker sales koto", "get_sales_summary")
    bank = lexicon.load(session).examples
    assert "aajker sales koto" in bank
    assert bank["aajker sales koto"].tool_name == "get_sales_summary"


def test_retiring_takes_it_out_again(session, users) -> None:
    example_id = approve(session, users, "aajker sales koto", "get_sales_summary")
    vocabulary.retire_example(session, users["ceo"], example_id)
    session.commit()
    lexicon.invalidate_cache()
    assert lexicon.load(session).examples == {}


def test_the_bank_is_keyed_on_the_normalised_question(session, users) -> None:
    """So a taught question and a typed one meet however it was punctuated."""
    approve(session, users, "Aajker sales koto??", "get_sales_summary")
    assert "aajker sales koto" in lexicon.load(session).examples


# --------------------------------------------------------------------------
# The deterministic shortcut
# --------------------------------------------------------------------------


def test_an_example_names_the_intent_when_detection_found_none(
    client, agent_engine, users,
) -> None:
    question = "purono hisheb ta ekbar mile dekhao to"

    def ask() -> dict:
        return client.post("/api/chat", json={"message": question},
                           headers={"X-User": "ceo"}).json()

    assert ask()["intent"] == "UNKNOWN"

    with Session(agent_engine, expire_on_commit=False) as db:
        approve(db, users, question, "get_sales_summary",
                intent="SALES_SUMMARY")

    assert ask()["intent"] == "SALES_SUMMARY"


def test_an_example_never_overrides_an_intent_that_was_detected(
    client, agent_engine, users,
) -> None:
    """The same rule the aliases follow.

    What the shipped classifier worked out for itself is never second-guessed;
    an example speaks only where the rules came back UNKNOWN.
    """
    question = "total sales this month"

    with Session(agent_engine, expire_on_commit=False) as db:
        approve(db, users, question, "get_stock_summary", intent="STOCK_SUMMARY")

    answer = client.post("/api/chat", json={"message": question},
                         headers={"X-User": "ceo"}).json()
    assert answer["intent"] == "SALES_SUMMARY"


def test_using_an_example_counts_against_it(client, agent_engine, users) -> None:
    """A bank nobody hits is a bank to prune, and the count is how to know."""
    question = "purono hisheb ta ekbar mile dekhao to"
    with Session(agent_engine, expire_on_commit=False) as db:
        example_id = approve(db, users, question, "get_sales_summary",
                             intent="SALES_SUMMARY")

    client.post("/api/chat", json={"message": question},
                headers={"X-User": "ceo"})

    with Session(agent_engine) as db:
        used = db.get(AgentExample, example_id).use_count
    assert used >= 1


def test_an_example_naming_a_retired_intent_is_ignored(session, users) -> None:
    """Ignored, not raised: the question stays as unclassified as it was."""
    from app.ai.orchestrator import Orchestrator
    from app.ai.intent import detect_intent

    orchestrator = Orchestrator(session, users["ceo"])
    orchestrator.lexicon = Lexicon(examples={
        "kichu ekta": LearnedExample(1, "get_sales_summary", "NO_SUCH_INTENT",
                                     "kichu ekta"),
    })
    prediction = detect_intent("kichu ekta")
    assert orchestrator._apply_example("kichu ekta", prediction).intent.value == (
        "UNKNOWN"
    )


def test_an_example_naming_a_tool_outside_the_intent_is_not_used(
    session, users,
) -> None:
    """The intent's allow-list still bounds the choice.

    An approved example refines *which* report answers a question; it does not
    let one intent reach another intent's tools.
    """
    from app.ai.orchestrator import Orchestrator
    from app.ai.schemas import Intent, StructuredQuery

    orchestrator = Orchestrator(session, users["ceo"])
    orchestrator.lexicon = Lexicon(examples={
        "total sales this month": LearnedExample(
            1, "get_material_stock_summary", "STOCK_SUMMARY",
            "total sales this month",
        ),
    })
    query = StructuredQuery(intent=Intent.SALES_SUMMARY, language="en")
    chosen = orchestrator._select_tool(query, "total sales this month", ())
    assert chosen != "get_material_stock_summary"


def test_the_stored_arguments_are_never_replayed(session, users) -> None:
    """What reaches the tool is built from this question, not the old one."""
    row = vocabulary.propose_example(
        session, users["ceo"], question="aajker sales koto",
        tool_name="get_sales_summary",
        arguments={"date_from": "2020-01-01", "date_to": "2020-01-31",
                   "filters": {"region_code": ["SOMEONE-ELSES"]}},
    )
    vocabulary.approve_example(session, users["ceo"], row["example_id"])
    session.commit()
    lexicon.invalidate_cache()

    learned = lexicon.load(session).examples["aajker sales koto"]
    # The bank carries the tool and the intent, and nothing that could place a
    # past caller's scope on a new question.
    assert learned.tool_name == "get_sales_summary"
    assert not hasattr(learned, "arguments")
