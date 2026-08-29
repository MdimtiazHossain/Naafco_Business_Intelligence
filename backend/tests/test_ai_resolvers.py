"""Date resolution, intent detection, language detection and entity resolution."""

from __future__ import annotations

import datetime as dt

import pytest
from conftest_phase2 import make_material

from app.ai.date_resolver import DateResolver, normalize_digits
from app.ai.entity_resolver import EntityResolver
from app.ai.exceptions import AmbiguousEntityError, EntityNotFoundError
from app.ai.intent import detect_intent, detect_language
from app.ai.schemas import DateRangeType, EntityType, GroupBy, Intent

TODAY = dt.date(2026, 8, 15)   # a Saturday, mid-month, inside FY 2026-27


@pytest.fixture
def resolver() -> DateResolver:
    return DateResolver(today=TODAY)


# --------------------------------------------------------------------------
# Language
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What is today's sales?", "en"),
        ("আজকের বিক্রয় কত?", "bn"),
        ("আজকের sales কত?", "mixed"),
        ("Dhaka region-এর sales দেখাও", "mixed"),
    ],
)
def test_language_detection(text: str, expected: str) -> None:
    assert detect_language(text) == expected


def test_bangla_digits_are_normalised() -> None:
    assert normalize_digits("১৫ দিন") == "15 দিন"
    assert normalize_digits("Top ২০") == "Top 20"


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_from", "expected_to"),
    [
        ("today's sales", TODAY, TODAY),
        ("আজকের sales কত?", TODAY, TODAY),
        ("yesterday sales", dt.date(2026, 8, 14), dt.date(2026, 8, 14)),
        ("গতকালের sales", dt.date(2026, 8, 14), dt.date(2026, 8, 14)),
        ("this month sales", dt.date(2026, 8, 1), TODAY),
        ("এই মাসের sales", dt.date(2026, 8, 1), TODAY),
        ("last month sales", dt.date(2026, 7, 1), dt.date(2026, 7, 31)),
        ("গত মাসের sales", dt.date(2026, 7, 1), dt.date(2026, 7, 31)),
        ("this week", dt.date(2026, 8, 10), TODAY),
        ("গত সপ্তাহ", dt.date(2026, 8, 3), dt.date(2026, 8, 9)),
        ("last 30 days", dt.date(2026, 7, 17), TODAY),
        ("গত ৩০ দিনের sales", dt.date(2026, 7, 17), TODAY),
        ("last 7 days", dt.date(2026, 8, 9), TODAY),
        ("MTD sales", dt.date(2026, 8, 1), TODAY),
    ],
)
def test_period_phrases(resolver: DateResolver, text: str, expected_from: dt.date,
                        expected_to: dt.date) -> None:
    resolved = resolver.resolve(text)
    assert (resolved.date_from, resolved.date_to) == (expected_from, expected_to)


def test_financial_year_uses_the_configured_start_month(resolver: DateResolver) -> None:
    """The company financial year starts in July, so YTD is not calendar YTD."""
    ytd = resolver.resolve("YTD sales")
    assert ytd.date_from == dt.date(2026, 7, 1)
    assert ytd.financial_year == "FY 2026-27"


def test_this_year_is_the_whole_financial_year_not_year_to_date(
    resolver: DateResolver,
) -> None:
    """"This Year" and "YTD" are different questions and must answer differently.

    They shared a branch, so both returned the financial year *so far* under the
    label "FY 2026-27 to date". Two options in the dashboard's date filter gave
    the identical window, and the one named after the year stopped at today.
    """
    this_year = resolver.of_type(DateRangeType.THIS_YEAR)
    ytd = resolver.of_type(DateRangeType.YTD)

    assert (this_year.date_from, this_year.date_to) == (
        dt.date(2026, 7, 1), dt.date(2027, 6, 30))
    assert this_year.label == "FY 2026-27"

    assert (ytd.date_from, ytd.date_to) == (dt.date(2026, 7, 1), TODAY)
    assert ytd.label == "FY 2026-27 to date"

    assert this_year.date_to != ytd.date_to
    # …and "This Year" covers a whole year, the way "Last Year" already did.
    last_year = resolver.of_type(DateRangeType.LAST_YEAR)
    assert (last_year.date_to - last_year.date_from).days == (
        this_year.date_to - this_year.date_from).days


@pytest.mark.parametrize("today, expected_label, expected_from, expected_to", [
    # The last day of FY 2025-26 still belongs to it…
    (dt.date(2026, 6, 30), "FY 2025-26", dt.date(2025, 7, 1), dt.date(2026, 6, 30)),
    # …and the next day is the first of FY 2026-27.
    (dt.date(2026, 7, 1), "FY 2026-27", dt.date(2026, 7, 1), dt.date(2027, 6, 30)),
])
def test_this_year_rolls_over_on_the_financial_year_boundary(
    today: dt.date, expected_label: str,
    expected_from: dt.date, expected_to: dt.date,
) -> None:
    resolved = DateResolver(today=today).of_type(DateRangeType.THIS_YEAR)
    assert (resolved.date_from, resolved.date_to) == (expected_from, expected_to)
    assert resolved.label == expected_label


def test_explicit_financial_year(resolver: DateResolver) -> None:
    resolved = resolver.resolve("FY 2026-27 sales")
    assert resolved.type is DateRangeType.FINANCIAL_YEAR
    assert resolved.date_from == dt.date(2026, 7, 1)
    assert resolved.date_to == dt.date(2027, 6, 30)
    assert resolved.label == "FY 2026-27"


def test_future_financial_year_is_not_hardcoded(resolver: DateResolver) -> None:
    resolved = resolver.resolve("FY 2031-32 sales")
    assert resolved.date_from == dt.date(2031, 7, 1)
    assert resolved.date_to == dt.date(2032, 6, 30)


def test_explicit_date_range(resolver: DateResolver) -> None:
    resolved = resolver.resolve("sales from 2026-08-01 to 2026-08-10")
    assert resolved.type is DateRangeType.CUSTOM
    assert (resolved.date_from, resolved.date_to) == (dt.date(2026, 8, 1),
                                                      dt.date(2026, 8, 10))


def test_this_month_compares_against_the_same_span_of_last_month(
    resolver: DateResolver,
) -> None:
    """MTD must be compared like for like, not against a whole month."""
    resolved = resolver.resolve("this month")
    assert resolved.compare_from == dt.date(2026, 7, 1)
    assert resolved.compare_to == dt.date(2026, 7, 15)


def test_last_month_compares_against_the_month_before(resolver: DateResolver) -> None:
    resolved = resolver.resolve("last month")
    assert (resolved.compare_from, resolved.compare_to) == (dt.date(2026, 6, 1),
                                                            dt.date(2026, 6, 30))


def test_no_period_falls_back_to_the_default(resolver: DateResolver) -> None:
    resolved = resolver.resolve("region wise sales")
    assert resolved.type is DateRangeType.THIS_MONTH


def test_detect_returns_none_when_no_period_is_mentioned(resolver: DateResolver) -> None:
    assert resolver.detect("region wise sales") is None
    assert resolver.detect("আজকের sales") is DateRangeType.TODAY


# --------------------------------------------------------------------------
# Intent
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("আজকের sales কত?", Intent.SALES_SUMMARY),
        ("এই মাসের sales কত?", Intent.SALES_SUMMARY),
        ("What is today's sales?", Intent.SALES_SUMMARY),
        ("গত মাসের তুলনায় sales কত বেড়েছে?", Intent.SALES_GROWTH),
        ("show sales trend for last 30 days", Intent.SALES_TREND),
        ("Region-wise sales দেখাও", Intent.REGION_PERFORMANCE),
        # A general product question ranks brands; only an explicit SKU
        # question ranks SKUs.
        ("Product-wise sales দেখাও", Intent.MATERIAL_BRAND_PERFORMANCE),
        ("Top products দেখাও", Intent.MATERIAL_BRAND_PERFORMANCE),
        ("Brand-wise sales দেখাও", Intent.MATERIAL_BRAND_PERFORMANCE),
        ("Top SKU দেখাও", Intent.MATERIAL_PERFORMANCE),
        ("SKU-wise sales দেখাও", Intent.MATERIAL_PERFORMANCE),
        ("Sales by territory", Intent.TERRITORY_PERFORMANCE),
        ("sales by sales officer", Intent.SALES_FORCE_PERFORMANCE),
        ("Total stock কত?", Intent.STOCK_SUMMARY),
        ("Plant-wise stock দেখাও", Intent.STOCK_BY_PLANT),
        ("Storage location wise stock", Intent.STOCK_BY_STORAGE_LOCATION),
        ("Material group wise stock", Intent.STOCK_BY_MATERIAL_GROUP),
        ("Material brand wise stock", Intent.STOCK_BY_MATERIAL_BRAND),
        # A bare "brand" asked of stock can only mean the material brand: the
        # stock view carries no sales brand.
        ("stock by brand", Intent.STOCK_BY_MATERIAL_BRAND),
        # The longer phrases above must be consumed whole: a material-group
        # question answered material by material is the wrong report.
        ("Material wise stock", Intent.STOCK_BY_MATERIAL),
        ("stock by material code", Intent.STOCK_BY_MATERIAL),
        ("Which stock is expiring soon?", Intent.EXPIRING_STOCK),
        ("এই মাসে target achievement কত?", Intent.TARGET_ACHIEVEMENT),
        ("Target gap কত?", Intent.TARGET_GAP),
        ("Why is sales down?", Intent.ROOT_CAUSE_ANALYSIS),
        ("Give me today's management summary", Intent.BUSINESS_SUMMARY),
        ("show me business alerts", Intent.BUSINESS_ALERT),
    ],
)
def test_intent_detection(question: str, expected: Intent) -> None:
    assert detect_intent(question).intent is expected


def test_stock_measure_is_extracted_only_for_stock_questions() -> None:
    """Naming a category ranks by it; a question naming none ranks by the total.

    The last case is the guard: the four category words belong to stock, so
    they are only read when the question is a stock question. A sales answer
    must never come back ranked by a stock category.
    """
    assert detect_intent(
        "which material has the highest blocked stock?").stock_measure == "blocked_stock"
    assert detect_intent(
        "material wise unrestricted stock").stock_measure == "unrestricted_stock"
    assert detect_intent("total stock কত?").stock_measure is None
    assert detect_intent("region wise sales").stock_measure is None


def test_limit_is_extracted() -> None:
    assert detect_intent("Top 20 customer দেখাও").limit == 20
    assert detect_intent("show top 50 products").limit == 50


def test_a_receivables_question_is_recognised_and_a_collections_one_is_not() -> None:
    """The test an intent has to pass is whether a tool can answer it.

    Revision 0031 gave receivables a source, so outstanding, overdue and aging
    route to the credit tools. A *collections* question still has no source — no
    extract states individual payment transactions — so it stays unrecognised
    and falls through to the sales default, where the agent answers with what it
    has rather than routing to a tool that would return nothing.

    The distinction matters more than it looks: an invoice's total paid amount
    is sitting right there and is not a collection, so the cheap mistake here is
    to answer a collections question with it.
    """
    from app.ai.intent import METRIC_KEYWORDS

    assert set(METRIC_KEYWORDS) == {"sales", "stock", "target", "credit"}

    for question in ("Total outstanding কত?", "Outstanding aging দেখাও",
                     "which customers are overdue?", "বকেয়া কত?"):
        assert detect_intent(question).metric == "credit", question

    # No "collection" metric, and the word must not drag a question into credit
    # by accident either.
    assert "collection" not in METRIC_KEYWORDS
    prediction = detect_intent("আজকের collection কত?")
    assert prediction.metric != "collection"


def test_the_credit_intents_split_by_what_was_asked() -> None:
    """Three questions, three tools — a bare total is not a customer ranking."""
    assert detect_intent("total outstanding").intent is Intent.CREDIT_SUMMARY
    assert detect_intent("outstanding aging buckets").intent is Intent.CREDIT_AGING
    assert detect_intent("which customers are overdue").intent is Intent.CREDIT_OVERDUE


def test_thresholds_are_extracted() -> None:
    prediction = detect_intent("which territory is below 80%?")
    assert prediction.percent_threshold == 80.0
    prediction = detect_intent("৩০ দিনের মধ্যে expire হবে এমন stock দেখাও")
    assert prediction.days_threshold == 30


def test_group_by_detection_prefers_the_more_specific_phrase() -> None:
    assert detect_intent("sub territory wise sales").group_by[0] is GroupBy.SUB_TERRITORY
    assert detect_intent("territory wise sales").group_by[0] is GroupBy.TERRITORY


def test_unknown_question_is_not_forced_into_an_intent() -> None:
    assert detect_intent("what is the weather in Dhaka tomorrow").intent in (
        Intent.UNKNOWN, Intent.SALES_SUMMARY
    )
    assert detect_intent("hello").intent is Intent.UNKNOWN


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------


def test_region_name_resolves_to_the_official_master_record(session) -> None:
    resolver = EntityResolver(session)
    entity = resolver.resolve_term("Dhaka")
    assert entity.entity_type is EntityType.REGION
    assert entity.code == "REG001"
    assert entity.match == "exact_name"


def test_code_resolves_directly(session) -> None:
    resolver = EntityResolver(session)
    assert resolver.resolve_term("Z001").entity_type is EntityType.ZONE
    assert resolver.resolve_term("SKU001").entity_type is EntityType.MATERIAL


def test_unknown_name_raises_rather_than_inventing(session) -> None:
    resolver = EntityResolver(session)
    with pytest.raises(EntityNotFoundError) as exc:
        resolver.resolve_term("Atlantis")
    assert "Atlantis" in exc.value.user_message
    assert "official master data" in exc.value.user_message


def test_entities_are_found_inside_a_question(session) -> None:
    resolver = EntityResolver(session)
    entities = resolver.resolve_message("এই মাসে Dhaka region-এর sales দেখাও")
    assert [(e.entity_type, e.code) for e in entities] == [
        (EntityType.REGION, "REG001")
    ]


def test_stopwords_are_not_treated_as_entities(session) -> None:
    resolver = EntityResolver(session)
    assert resolver.resolve_message("show me sales by region this month") == []


def test_ambiguous_name_asks_instead_of_guessing(session) -> None:
    """A name shared by a material and a customer must trigger a clarification."""
    from app.database.models_warehouse import DimCustomer

    session.add(make_material("SKU-ABC", "ABC"))
    session.add(DimCustomer(customer_code="CUST-ABC", customer_name="ABC"))
    session.commit()

    resolver = EntityResolver(session)
    with pytest.raises(AmbiguousEntityError) as exc:
        resolver.resolve_term("ABC")
    message = exc.value.user_message
    assert message.startswith("Do you mean")
    assert "material" in message and "customer" in message
    assert len(exc.value.details["candidates"]) == 2


def test_longer_phrases_win_over_shorter_ones(session) -> None:
    from app.database.models import DimTerritory

    # TR002 is Khulna's territory in the shared fixture, so this one needs a
    # code of its own; the test is about the *name* winning the longer match.
    session.add(DimTerritory(territory_code="TR010", territory_name="Dhaka North",
                             unit_code="UN001"))
    session.commit()

    resolver = EntityResolver(session)
    entities = resolver.resolve_message("Dhaka North sales")
    assert [(e.entity_type, e.code) for e in entities] == [
        (EntityType.TERRITORY, "TR010")
    ]


# --------------------------------------------------------------------------
# A question word is never a place
# --------------------------------------------------------------------------


def test_a_bangla_question_word_is_not_a_sub_territory(session) -> None:
    """The bug this guards: "sales koto?" reported one sub-territory's sales.

    ``koto`` is Bangla for "how much" and the ordinary way to ask the question
    in a mixed-language message. It is also the first four letters of the real
    sub-territory "South Kotowali", so it matched, the answer was silently
    narrowed to that one sub-territory, and the figure looked plausible.
    """
    from app.database.models import DimSubTerritory

    session.add(DimSubTerritory(sub_territory_code="STR-KOT",
                                sub_territory_name="South Kotowali",
                                territory_code="TR001"))
    session.commit()

    resolver = EntityResolver(session)
    assert resolver.resolve_message("last month total sales koto") == []
    assert resolver.resolve_message("গত মাসের সেলস কত?") == []
    # The place itself is still reachable by its real name.
    assert [e.code for e in resolver.resolve_message("Kotowali sales")] == ["STR-KOT"]


def test_a_fragment_inside_a_word_is_not_a_match(session) -> None:
    """"Sachet" must not answer to "ache": a partial match starts a word."""
    session.add(make_material("SKU-SACHET", "Aquavit F Sachet 1kg"))
    session.commit()

    resolver = EntityResolver(session)
    assert resolver.candidates("ache") == []
    # Anchored at a word start, the useful abbreviation still resolves.
    assert [e.code for e in resolver.candidates("sach")] == ["SKU-SACHET"]
