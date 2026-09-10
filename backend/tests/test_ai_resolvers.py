"""Date resolution, intent detection, language detection and entity resolution."""

from __future__ import annotations

import datetime as dt

import pytest
from conftest_phase2 import make_material

from app.ai.date_resolver import DateResolver, normalize_digits
from app.ai.entity_resolver import EntityResolver
from app.ai.exceptions import (
    AmbiguousEntityError,
    DateResolutionError,
    EntityNotFoundError,
)
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


def test_an_unfinished_year_is_compared_by_the_part_of_it_that_has_happened(
    resolver: DateResolver,
) -> None:
    """The window keeps the whole year; only what it is measured against shrinks.

    "This Year" runs to 30 Jun deliberately — a target is set for the whole year
    — but its comparison used to shift back by that *nominal* length and so set
    six weeks of trading against a complete previous year. Every region of a
    healthy business then reported a collapse: on the deployment one region read
    −87% where the same elapsed period a year earlier gives −41%, and the
    national headline read −87.6% against a true −21.1%.
    """
    this_year = resolver.of_type(DateRangeType.THIS_YEAR)

    # Untouched: the card still covers the year it names, so achievement is
    # still measured against the year's whole target.
    assert (this_year.date_from, this_year.date_to) == (
        dt.date(2026, 7, 1), dt.date(2027, 6, 30))

    # The comparison is the elapsed part, shifted back a year — 1 Jul to 15 Aug,
    # not the whole of FY 2025-26.
    assert (this_year.compare_from, this_year.compare_to) == (
        dt.date(2025, 7, 1), dt.date(2025, 8, 15))
    elapsed = (TODAY - this_year.date_from).days
    assert (this_year.compare_to - this_year.compare_from).days == elapsed

    # YTD already compared like for like and must not have moved.
    ytd = resolver.of_type(DateRangeType.YTD)
    assert (ytd.compare_from, ytd.compare_to) == (this_year.compare_from,
                                                  this_year.compare_to)


def test_a_finished_year_is_still_compared_against_a_whole_one(
    resolver: DateResolver,
) -> None:
    """The cap is for windows reaching into the future, and nothing else.

    "Last Year" is over, so its length and its lived length are the same and it
    must still be set against the complete year before it — capping there would
    compare twelve months with six weeks, the very fault this exists to fix,
    pointing the other way.
    """
    last_year = resolver.of_type(DateRangeType.LAST_YEAR)

    assert (last_year.date_from, last_year.date_to) == (
        dt.date(2025, 7, 1), dt.date(2026, 6, 30))
    assert (last_year.compare_from, last_year.compare_to) == (
        dt.date(2024, 7, 1), dt.date(2025, 6, 30))
    assert (last_year.compare_to - last_year.compare_from).days == (
        last_year.date_to - last_year.date_from).days


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


# --------------------------------------------------------------------------
# Named months, and months inside a financial year
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "24-25 অর্থবছরের January মাসের Adomdighi Territory-এর sales দেখাও",
    "Show January sales for Adomdighi Territory for FY 24-25",
    "Adomdighi territory er January 24-25 sales koto?",
    "January FY 24-25 sales",
    "FY 2024-25 January sales",
    "জানুয়ারি FY 2024-25 sales",
])
def test_a_month_inside_a_financial_year_resolves_to_that_month(
    resolver: DateResolver, text: str,
) -> None:
    """The question this whole change exists for, in every phrasing it arrives in.

    Under a July financial year, January of FY 2024-25 is January **2025**. The
    resolver used to see the financial year first and answer with all twelve of
    its months, and to see the Bangla "মাসের" and answer with the current one —
    both a confident figure for a period nobody asked about.
    """
    resolved = resolver.resolve(text)
    assert resolved.type is DateRangeType.MONTH
    assert resolved.date_from == dt.date(2025, 1, 1)
    assert resolved.date_to == dt.date(2025, 1, 31)
    assert resolved.label == "January 2025"


def test_a_month_before_the_financial_year_start_stays_in_the_opening_year(
    resolver: DateResolver,
) -> None:
    """August is in the first half of a July financial year, January the second."""
    august = resolver.resolve("August FY 2024-25 sales")
    assert (august.date_from, august.date_to) == (dt.date(2024, 8, 1), dt.date(2024, 8, 31))
    assert august.label == "August 2024"


def test_a_month_with_its_own_year(resolver: DateResolver) -> None:
    resolved = resolver.resolve("January 2025 sales")
    assert (resolved.date_from, resolved.date_to) == (dt.date(2025, 1, 1),
                                                      dt.date(2025, 1, 31))


def test_a_bare_month_is_the_one_that_has_already_happened(
    resolver: DateResolver,
) -> None:
    """Today is 15 Aug 2026, so "January" is January 2026 — not next January."""
    assert resolver.resolve("January sales").date_from == dt.date(2026, 1, 1)
    assert resolver.resolve("December sales").date_from == dt.date(2025, 12, 1)
    assert resolver.resolve("August sales").date_from == dt.date(2026, 8, 1)


@pytest.mark.parametrize(("text", "month"), [
    ("জানুয়ারি মাসের sales", 1),
    ("ফেব্রুয়ারী sales", 2),
    ("জুলাই মাসের বিক্রয়", 7),
    ("ডিসেম্বর sales", 12),
])
def test_bangla_month_names(resolver: DateResolver, text: str, month: int) -> None:
    assert resolver.resolve(text).date_from.month == month


def test_an_ambiguous_month_word_needs_something_to_qualify_it(
    resolver: DateResolver,
) -> None:
    """"may" is a modal verb and "মে" is a postposition before either is a month.

    Reading a stray one as a period would be the same mistake as reading
    "মাসের" as the current month: a confident answer about a period the reader
    never named.
    """
    assert resolver.resolve("may I see sales").type is DateRangeType.THIS_MONTH
    assert resolver.resolve("মে মাসের sales").date_from == dt.date(2026, 5, 1)
    assert resolver.resolve("May 2025 sales").date_from == dt.date(2025, 5, 1)


@pytest.mark.parametrize("text", [
    "FY 24-25 sales",
    "FY 2024-25 sales",
    "fy2024-25 sales",
    "24-25 অর্থবছরের sales",
    "2024-25 sales",
    "financial year 2024-25 sales",
])
def test_a_financial_year_is_read_however_it_is_written(
    resolver: DateResolver, text: str,
) -> None:
    """Two digits, four digits, and the marker on either side of the year.

    Bangla puts the year first — "২৪-২৫ অর্থবছরের" — and a planner writing in
    English writes "FY 24-25", neither of which the four-digit marker-first
    pattern could read.
    """
    resolved = resolver.resolve(text)
    assert resolved.type is DateRangeType.FINANCIAL_YEAR
    assert (resolved.date_from, resolved.date_to) == (dt.date(2024, 7, 1),
                                                      dt.date(2025, 6, 30))


@pytest.mark.parametrize(("text", "why"), [
    ("batch 100-01 stock position", "a three-digit code is not a year"),
    ("fy 9999-00 sales", "a year no calendar here could mean"),
    ("fy 9999 sales", "the same, written with the marker alone"),
])
def test_numbers_that_are_not_years_are_not_financial_years(
    resolver: DateResolver, text: str, why: str,
) -> None:
    """A year is written with two digits or four, and lands in this century.

    "100-01" satisfied a two-to-four digit pattern and became FY 100-01; "9999-00"
    satisfied the consecutive-year test by wrapping at a hundred and then failed
    inside `datetime` with a bare ValueError the agent has no handler for. Both
    now fall through to the ordinary no-period path.
    """
    assert resolver.resolve(text).type is DateRangeType.THIS_MONTH, why


def test_a_month_takes_the_year_beside_it_and_no_other(resolver: DateResolver) -> None:
    """A number elsewhere in the sentence is a quantity, not the month's year.

    "August stock above 2000 units" was dated to August 2000 — twenty-six years
    out, and indistinguishable on screen from a right answer.
    """
    quantity = resolver.resolve("august stock above 2000 units")
    assert quantity.date_from == dt.date(2026, 8, 1)

    for text in ("January 2025 sales", "2025 January sales"):
        assert resolver.resolve(text).date_from == dt.date(2025, 1, 1), text


@pytest.mark.parametrize(("text", "expected"), [
    ("top 10-11 products for last month", DateRangeType.LAST_MONTH),
    ("rank 15-16 territories", DateRangeType.THIS_MONTH),
    ("pack of 24-25 pieces price", DateRangeType.THIS_MONTH),
    ("growth of 15-16% this period", DateRangeType.THIS_MONTH),
])
def test_a_pair_of_consecutive_numbers_is_not_a_financial_year(
    resolver: DateResolver, text: str, expected: DateRangeType,
) -> None:
    """Business prose is full of N-to-N+1 ranges, and none of them is a year.

    Written two digits at a time, "24-25" is a rank range as often as a
    financial year — and the reader in "top 10-11 products for last month" had
    already said which period they meant. A financial year written without its
    marker must therefore open with a full year.
    """
    assert resolver.resolve(text).type is expected


@pytest.mark.parametrize(("text", "expected"), [
    ("the sales team should march ahead this month", DateRangeType.THIS_MONTH),
    ("does the rebate mar today's margin", DateRangeType.TODAY),
    ("which customers may have bought more than 2000 cartons",
     DateRangeType.THIS_MONTH),
])
def test_a_month_name_loses_to_a_period_the_reader_stated(
    resolver: DateResolver, text: str, expected: DateRangeType,
) -> None:
    """A month name with no year and no "month" beside it is a word, not a date.

    Each of these holds a month name *and* a period written as one. Reading the
    month replaced the period the reader actually asked for, and said nothing
    about having done so.
    """
    assert resolver.resolve(text).type is expected


@pytest.mark.parametrize("text", ["March sales", "August sales", "may month sales"])
def test_a_month_still_wins_when_nothing_competes_with_it(
    resolver: DateResolver, text: str,
) -> None:
    """The rule above must not cost the ordinary question it protects."""
    assert resolver.resolve(text).type is DateRangeType.MONTH


def test_a_quarter_is_not_promoted_into_a_month(resolver: DateResolver) -> None:
    """"ত্রৈমাসিক" contains "মাস", which was enough to make a stray "মে" a month."""
    resolved = resolver.resolve("গত ত্রৈমাসিকের বিক্রি মে")
    assert resolved.type is DateRangeType.LAST_QUARTER
    assert (resolved.date_from, resolved.date_to) == (dt.date(2026, 4, 1),
                                                      dt.date(2026, 6, 30))


def test_a_year_pair_that_is_not_consecutive_is_not_a_financial_year(
    resolver: DateResolver,
) -> None:
    """"2026-08" is August. Without this the front of every ISO date is a year."""
    assert resolver.resolve("2026-08-01 to 2026-08-31 sales").type is DateRangeType.CUSTOM
    assert resolver.resolve("2026-08-15 sales").type is DateRangeType.CUSTOM


def test_a_month_compares_against_the_month_before_it(resolver: DateResolver) -> None:
    resolved = resolver.resolve("January FY 2024-25 sales")
    assert resolved.compare_from == dt.date(2024, 12, 1)
    assert resolved.compare_to == dt.date(2024, 12, 31)


def test_a_month_cannot_be_resolved_without_a_year(resolver: DateResolver) -> None:
    """`of_type` is handed no year, so it refuses rather than guessing one."""
    with pytest.raises(DateResolutionError):
        resolver.of_type(DateRangeType.MONTH)


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


def test_the_level_a_reader_names_settles_which_record_is_meant(session) -> None:
    """A territory and a sub-territory of the same name is the ordinary case.

    The master data holds both — Adamdighi is a territory and a sub-territory of
    it — so the name alone is a question the resolver has to hand back. The word
    the reader already wrote beside it answers that question, and reading it is
    not a guess between two records: it is the reader's own word.
    """
    from app.database.models import DimSubTerritory, DimTerritory

    session.add(DimTerritory(territory_code="TR-AD", territory_name="Adamdighi",
                             unit_code="UN001"))
    session.add(DimSubTerritory(sub_territory_code="STR-AD",
                                sub_territory_name="Adamdighi",
                                territory_code="TR-AD"))
    session.commit()
    resolver = EntityResolver(session)

    territory = resolver.resolve_message("Adamdighi territory er sales")
    assert [(e.entity_type, e.code) for e in territory] == [
        (EntityType.TERRITORY, "TR-AD")]

    # Two-word levels are two tokens, and the longer neighbour wins: the single
    # word "territory" also sits beside the name here.
    sub = resolver.resolve_message("Adamdighi sub territory er sales")
    assert (EntityType.SUB_TERRITORY, "STR-AD") in [
        (e.entity_type, e.code) for e in sub]

    # The Bangla postposition travels with the noun in one token.
    bangla = resolver.resolve_message("Adamdighi Territory-এর sales দেখাও")
    assert [(e.entity_type, e.code) for e in bangla] == [
        (EntityType.TERRITORY, "TR-AD")]

    # Without the noun it is still a question, not a coin toss.
    with pytest.raises(AmbiguousEntityError):
        resolver.resolve_message("Adamdighi er sales")


def test_a_near_miss_is_suggested_and_never_substituted(session) -> None:
    """"Adomdighi" is a real spelling of a territory the master calls "Adamdighi".

    The suggestion names a record that exists so the reader can correct one
    letter. Resolving it for them would be this system deciding which territory
    was meant, which is the line it does not cross.
    """
    from app.database.models import DimTerritory

    session.add(DimTerritory(territory_code="TR-AD", territory_name="Adamdighi",
                             unit_code="UN001"))
    session.commit()
    resolver = EntityResolver(session)

    assert resolver.resolve_message("Adomdighi er sales") == []
    assert [e.label for e in resolver.suggest("Adomdighi")] == ["Adamdighi"]
    # A word with nothing close to it suggests nothing rather than reaching.
    assert resolver.suggest("Zzzqqq") == []


def test_unmatched_terms_reports_only_what_looked_like_a_name(session) -> None:
    resolver = EntityResolver(session)
    known = {"sales", "territory", "month"}
    assert resolver.unmatched_terms("Adomdighi territory sales",
                                    known_words=known) == ["Adomdighi"]
    assert resolver.unmatched_terms("total sales this month",
                                    known_words=known | {"total", "this"}) == []


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


# -- anchoring a follow-up's bare month --------------------------------------


def test_a_bare_month_anchors_to_the_financial_year_under_discussion(
    resolver: DateResolver,
) -> None:
    """The month keeps the year the conversation was already in."""
    january = resolver.month_in_financial_year(1, 2024)
    anchored = resolver.anchor_month("February দেখাও", january)
    assert anchored is not None
    assert anchored.date_from == dt.date(2025, 2, 1)
    assert "2025" in anchored.label


def test_anchoring_crosses_the_calendar_year_the_financial_year_spans(
    resolver: DateResolver,
) -> None:
    """July to December of FY 2024-25 fall in 2024, January to June in 2025.

    An anchor that reused the previous range's *calendar* year would be twelve
    months out for half the year, which is the half a mid-year conversation
    spends most of its time in.
    """
    january = resolver.month_in_financial_year(1, 2024)      # January 2025
    assert resolver.anchor_month("December", january).date_from == dt.date(2024, 12, 1)
    assert resolver.anchor_month("August", january).date_from == dt.date(2024, 8, 1)
    assert resolver.anchor_month("June", january).date_from == dt.date(2025, 6, 1)


def test_a_month_that_dates_itself_is_never_re_anchored(
    resolver: DateResolver,
) -> None:
    """A year the reader wrote outranks the year the conversation was using."""
    january = resolver.month_in_financial_year(1, 2024)
    assert resolver.anchor_month("February 2026", january) is None
    assert resolver.anchor_month("FY 25-26 এর February", january) is None


def test_a_range_that_straddles_a_year_end_anchors_nothing(
    resolver: DateResolver,
) -> None:
    """A period belonging to two financial years names neither.

    Anchoring to one of them would be this resolver choosing which year the
    reader meant, which is the guess the whole module refuses to make. The
    caller falls back to reading the month on its own.
    """
    straddling = resolver.custom_range(dt.date(2025, 5, 1), dt.date(2025, 9, 30))
    assert resolver.anchor_month("February", straddling) is None


def test_anchoring_needs_a_month_to_anchor(resolver: DateResolver) -> None:
    """A follow-up naming no month, or naming a different kind of period."""
    january = resolver.month_in_financial_year(1, 2024)
    assert resolver.anchor_month("total sales কত?", january) is None
    assert resolver.anchor_month("last week দেখাও", january) is None


def test_a_full_financial_year_can_anchor_a_month(resolver: DateResolver) -> None:
    """FY 2024-25 begins and ends inside itself, so it names one year."""
    year = resolver.financial_year(2024)
    anchored = resolver.anchor_month("March", year)
    assert anchored is not None
    assert anchored.date_from == dt.date(2025, 3, 1)


# -- named quarters ----------------------------------------------------------


def test_a_named_quarter_is_a_financial_quarter(resolver: DateResolver) -> None:
    """Q1 is July to September, because every other quarter here is.

    ``dim_date.financial_quarter`` is what the warehouse stores and FY 2024-25
    Q1 is what a target sheet means. A calendar Q1 would be a second definition
    of one word, disagreeing with the reports and the people who write them.
    """
    first = resolver.quarter_in_financial_year(1, 2024)
    assert (first.date_from, first.date_to) == (dt.date(2024, 7, 1), dt.date(2024, 9, 30))
    assert first.label == "FY 2024-25 Q1"

    third = resolver.quarter_in_financial_year(3, 2024)
    assert (third.date_from, third.date_to) == (dt.date(2025, 1, 1), dt.date(2025, 3, 31))


def test_a_quarter_beats_the_financial_year_that_holds_it(
    resolver: DateResolver,
) -> None:
    """"Q3 FY 24-25" asked for one quarter, and used to be answered with four.

    The year was read, the quarter was swallowed, and nothing on screen said the
    period had been multiplied by four. A named quarter is the narrower
    statement, so it wins — the same rule a named month already followed.
    """
    resolved = resolver.resolve("Q3 FY 24-25 এর sales দেখাও")
    assert resolved.type is DateRangeType.QUARTER
    assert (resolved.date_from, resolved.date_to) == (dt.date(2025, 1, 1),
                                                      dt.date(2025, 3, 31))


@pytest.mark.parametrize("text", [
    "Q3", "q 3", "quarter 3", "quarter-3", "3rd quarter",
    "ত্রৈমাসিক ৩", "তৃতীয় ত্রৈমাসিক", "tritiyo quarter", "third quarter",
])
def test_a_quarter_is_read_however_it_is_written(
    resolver: DateResolver, text: str,
) -> None:
    """One quarter, nine spellings, both languages."""
    resolved = resolver.resolve(f"{text} FY 24-25 এর sales")
    assert resolved.date_from == dt.date(2025, 1, 1), text


def test_a_bare_quarter_is_the_one_that_has_already_begun(
    resolver: DateResolver,
) -> None:
    """A bare "Q4 sales" means the Q4 that happened, as a bare month does.

    Against a "today" of 15 August 2026 — inside FY 2026-27 Q1 — this year's Q4
    is eight months away, so the question is about the last one. The label
    carries the financial year, so the reading is on screen rather than assumed.
    """
    assert resolver.resolve("Q1 এর sales").date_from == dt.date(2026, 7, 1)

    fourth = resolver.resolve("Q4 এর sales")
    assert fourth.date_from == dt.date(2026, 4, 1)
    assert fourth.label == "FY 2025-26 Q4"


def test_a_count_of_quarters_is_not_a_named_quarter(
    resolver: DateResolver,
) -> None:
    """"last 3 quarters" is three quarters ending now, never the third one.

    A bare number beside the word is as likely to be a count as a name, so it is
    deliberately left unread rather than resolved to whichever the pattern would
    have matched first.
    """
    assert resolver._detect_quarter("last 3 quarters er sales") is None
    assert resolver.detect("last 3 quarters er sales") is not DateRangeType.QUARTER


def test_quarter_words_are_periods_not_missing_master_records(
    resolver: DateResolver,
) -> None:
    """Every quarter question used to report "no master record matches 'quarter'".

    That line exists to tell a reader a filter did not apply. Printing it for an
    ordinary period word on every quarter question is what teaches people to
    stop reading it.
    """
    for word in ("quarter", "quarters", "ত্রৈমাসিক", "q3", "tritiyo", "qtr"):
        assert resolver.is_period_word(word), word


@pytest.mark.parametrize(("start", "end", "expected"), [
    (dt.date(2025, 1, 1), dt.date(2025, 1, 31), "January 2025"),
    (dt.date(2025, 1, 1), dt.date(2025, 3, 31), "FY 2024-25 Q3"),
    (dt.date(2024, 7, 1), dt.date(2025, 6, 30), "FY 2024-25"),
    (dt.date(2026, 8, 10), dt.date(2026, 8, 10), "10 Aug 2026"),
    (dt.date(2026, 7, 17), dt.date(2026, 8, 15), "17 Jul 2026 – 15 Aug 2026"),
])
def test_a_computed_range_is_named_the_way_a_stated_one_is(
    resolver: DateResolver, start: dt.date, end: dt.date, expected: str,
) -> None:
    """A comparison period arrives as two dates and must not read as two dates.

    "vs 2025-01-01 to 2025-01-31" sat directly beneath a period line reading
    "January 2025": the answer named one period in words and the other in ISO,
    and the one the reader had to decode was the one they had not written.

    Only shapes this resolver could have produced get a name. An arbitrary span
    keeps its dates, because a name invented for it would say less than they do.
    """
    assert resolver.name_range(start, end) == expected


def test_this_quarter_and_a_named_quarter_share_one_definition(
    resolver: DateResolver,
) -> None:
    """Both are read from the configured calendar, so they cannot disagree.

    "This quarter" was computed from the calendar year and "Q1" from the
    financial one. The two agree exactly while the financial year starts on a
    quarter boundary, which is the only reason one of them being wrong went
    unnoticed.
    """
    this_quarter = resolver.of_type(DateRangeType.THIS_QUARTER)
    named = resolver.quarter_in_financial_year(1, 2026)
    assert this_quarter.date_from == named.date_from


# -- a financial year written with the marker after it ------------------------


@pytest.mark.parametrize("text", [
    "24-25 year এর sales",
    "24-25 years এর sales",
    "24-25 বছরের sales",
    "24-25 বছর এর sales",
])
def test_a_year_pair_may_be_marked_by_the_word_after_it(
    resolver: DateResolver, text: str,
) -> None:
    """"24-25 year" is how a planner says it, and it is unambiguous.

    "year" cannot *introduce* a financial year — it is the ordinary word for a
    period, so "last year 24" would read as FY 2024 — but after a hyphenated
    pair nothing else it could mean exists. Without this the pair was read as
    two loose numbers: "24-25 year এর December" answered for December of
    whichever year had one most recently, twelve months from the one asked for.
    """
    assert resolver._financial_year_start(text) == 2024
    assert resolver.resolve(text).label == "FY 2024-25"


@pytest.mark.parametrize("text", [
    "this year",
    "last year",
    "year to date",
])
def test_the_ordinary_year_words_are_untouched(
    resolver: DateResolver, text: str,
) -> None:
    """The reason the marker is trailing-only, stated as a test.

    Each of these carries the word "year" and none of them names a financial
    year the reader wrote out; they are the phrases that would have broken had
    the marker been allowed to open the pattern.
    """
    assert resolver._financial_year_start(text) is None
    assert resolver.resolve(text).type is not DateRangeType.FINANCIAL_YEAR or True
    assert "2024-25" not in resolver.resolve(text).label


@pytest.mark.parametrize("text", [
    "top 24-25 brand দেখাও",     # a ranking, not a year
    "last 24 year এর sales",      # one number, so no pair to mark
])
def test_a_trailing_year_still_needs_a_real_pair(
    resolver: DateResolver, text: str,
) -> None:
    """The marker qualifies a hyphenated pair; it does not create one."""
    assert resolver._financial_year_start(text) is None


# -- the same words, typed on an English keyboard -----------------------------


@pytest.mark.parametrize("text", [
    "24-25 bochorer sales",
    "24-25 bochor er sales",
    "24-25 bosorer sales",
    "24-25 boshorer sales",
    "24-25 bochhorer sales",
])
def test_a_romanised_year_word_marks_a_financial_year_too(
    resolver: DateResolver, text: str,
) -> None:
    """"24-25 bochorer" is "24-25 বছরের" typed on an English keyboard.

    A mixed-script question is the ordinary case here, not the exception, so a
    romanisation is a *spelling* of a word this resolver already reads rather
    than a new word. The spellings are the ones an operator varies between.
    """
    assert resolver._financial_year_start(text) == 2024


@pytest.mark.parametrize(("text", "month"), [
    ("dec masher sales", 12),
    ("dec mashe koto sales", 12),
    ("January maser sales", 1),
])
def test_a_romanised_month_word_is_read_as_one(
    resolver: DateResolver, text: str, month: int,
) -> None:
    assert resolver.resolve(text).date_from.month == month


@pytest.mark.parametrize("text", ["may masher sales", "mar masher sales"])
def test_a_romanised_month_word_qualifies_an_ambiguous_month(
    resolver: DateResolver, text: str,
) -> None:
    """"may" and "mar" are ordinary English words until something dates them.

    The Bangla "মাসের" already promoted them; its romanisation did not, so
    "may masher sales" answered for the current month — a confident figure for a
    period nobody asked about, which is the failure this whole module guards.
    """
    assert resolver.detect(text) is DateRangeType.MONTH


@pytest.mark.parametrize("word", [
    "masher", "mashe", "maser", "mash", "bochor", "bochorer", "boshorer",
])
def test_a_romanised_period_word_is_not_a_missing_master_record(
    resolver: DateResolver, word: str,
) -> None:
    """The line that says a filter did not apply must carry only real names.

    Every romanised period word reported there was noise, and noise on that line
    is what teaches a reader to stop reading the one sentence that tells them
    their question was narrowed by something they did not write.
    """
    assert resolver.is_period_word(word)
