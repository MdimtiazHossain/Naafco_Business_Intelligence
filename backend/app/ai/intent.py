"""Intent detection and language identification for English, Bangla and mixed text.

This is a deterministic classifier over keyword families. It runs on every
question — with or without an LLM — because it is fast, free, reproducible and
testable, and because it gives the orchestrator a reliable fallback when no
``OPENAI_API_KEY`` is configured.

When the LLM *is* available it proposes the intent, and this module still runs:
a disagreement is recorded rather than silently accepted, and the LLM can never
widen the query beyond what the structured schema allows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .date_resolver import normalize_digits
from .lexicon import EMPTY as LEXICON_EMPTY, Lexicon
from .schemas import GroupBy, Intent

BANGLA_RANGE = re.compile(r"[ঀ-৿]")
LATIN_RANGE = re.compile(r"[A-Za-z]")

Language = Literal["en", "bn", "mixed"]


def detect_language(text: str) -> Language:
    """Which script(s) the question uses. Mixed Bangla-English is the norm here."""
    has_bangla = bool(BANGLA_RANGE.search(text))
    has_latin = bool(LATIN_RANGE.search(text))
    if has_bangla and has_latin:
        return "mixed"
    if has_bangla:
        return "bn"
    return "en"


# --- keyword families -------------------------------------------------------

# No "collection" and no "outstanding" metric. Both datasets left this platform
# in revision 0020, and a metric word the agent recognises but cannot answer is
# worse than one it does not know: the question would route to a tool that no
# longer exists instead of falling through to "I don't report that".
METRIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sales": ("sales", "sale", "revenue", "turnover", "বিক্রয়", "বিক্রি", "সেলস"),
    "stock": ("stock", "inventory", "unrestricted", "blocked", "in transit",
              "quality inspection", "material group", "material code", "material",
              "plant", "storage location", "স্টক", "মজুদ", "মজুত", "ম্যাটেরিয়াল"),
    "target": ("target", "achievement", "budget", "টার্গেট", "লক্ষ্যমাত্রা", "অর্জন"),
}

MODIFIERS: dict[str, tuple[str, ...]] = {
    "trend": ("trend", "over time", "daily trend", "day wise", "day-wise", "ট্রেন্ড",
              "প্রবণতা"),
    "growth": ("growth", "grew", "increase", "increased", "decline", "declined", "vs",
               "versus", "compared", "comparison", "বেড়েছে", "কমেছে", "বৃদ্ধি", "তুলনায়",
               "বনাম"),
    "top": ("top", "best", "highest", "largest", "শীর্ষ", "সেরা", "বেশি"),
    "bottom": ("bottom", "worst", "lowest", "underperform", "under-perform", "below",
               "খারাপ", "কম", "নিচে"),
    "low": ("low", "less than", "below", "shortage", "urgent", "কম", "কমে", "স্বল্প"),
    # Expiry replaces the old coverage/low-stock vocabulary: the new data has
    # shelf lives, and no way to compute days of cover against sales.
    # Every phrase here must be specific to the *future* sense, because
    # "expiring" is tested before "expiry" and a phrase that also matched
    # "expired stock" would route a past-tense question to the wrong tool.
    # "expire হতে" / "expire হবে" carry the Bangla-English mix users actually
    # write; bare "expire" would match "expired" and cannot be added.
    "expiring": ("expiring soon", "expiring", "about to expire", "near expiry",
                 "will expire", "going to expire", "expires",
                 "মেয়াদ শেষ হতে", "মেয়াদোত্তীর্ণ হতে",
                 "expire হতে", "expire হবে"),
    "expiry": ("expired", "expiry", "expiration", "shelf life", "মেয়াদ",
               "মেয়াদোত্তীর্ণ"),
    "gap": ("gap", "shortfall", "ঘাটতি", "গ্যাপ"),
    "why": ("why", "reason", "cause", "root cause", "কেন", "কারণ"),
    "summary": ("summary", "overview", "snapshot", "management summary", "dashboard",
                "সারসংক্ষেপ", "সারাংশ", "ওভারভিউ"),
    "alert": ("alert", "warning", "risk", "attention", "অ্যালার্ট", "সতর্ক"),
    "detail": ("detail", "details", "breakdown", "wise", "list", "বিস্তারিত", "তালিকা"),
    "volume": ("volume", "volumes", "ভলিউম", "আয়তন"),
}

#: Unit words that make a question a volume question, and the unit they name.
#:
#: Deliberately *not* the alias table from :mod:`app.etl.uom`. That one parses a
#: pack-size field and can afford aliases like "l", "g" and "gr"; free text
#: cannot, because those letters appear inside ordinary words. These are matched
#: on word boundaries and kept conservative — a missed unit falls back to
#: reporting every unit, which is right, while a false match would silently
#: filter a report down to one.
VOLUME_UNIT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("KG", ("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "কেজি", "কিলো")),
    ("GM", ("gm", "gms", "gram", "grams", "গ্রাম")),
    ("LTR", ("ltr", "ltrs", "litre", "litres", "liter", "liters", "লিটার")),
    ("ML", ("ml", "mls", "millilitre", "millilitres", "মিলিলিটার")),
    ("PCS", ("pcs", "piece", "pieces", "পিস")),
)

#: Which of the four stock categories a question is about, most specific first.
#:
#: They are four different questions — "most blocked stock" is not "most stock" —
#: so naming one has to change the ranking, not just the wording of the answer.
#: A question that names none ranks by the total, which is the default.
#:
#: "transit" before "unrestricted" is not significant; "quality inspection"
#: before "quality" would be, so the longer phrase is listed first in each pair.
STOCK_MEASURE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quality_inspection_stock", ("quality inspection", "qi stock", "quality",
                                  "কোয়ালিটি")),
    ("stock_in_transit", ("in transit", "in-transit", "transit", "ট্রানজিট")),
    ("blocked_stock", ("blocked", "block stock", "ব্লকড")),
    ("unrestricted_stock", ("unrestricted", "sellable", "available stock",
                            "আনরেস্ট্রিক্টেড")),
)


#: Dimension nouns, most specific first so "sub territory" beats "territory".
GROUP_BY_KEYWORDS: tuple[tuple[GroupBy, tuple[str, ...]], ...] = (
    (GroupBy.SUB_TERRITORY, ("sub territory", "sub-territory", "subterritory",
                             "সাব টেরিটরি")),
    (GroupBy.TERRITORY, ("territory", "territories", "টেরিটরি")),
    (GroupBy.SALES_FORCE, ("sales force", "salesforce", "sales officer", "sr",
                           "officer", "সেলস অফিসার", "সেলসফোর্স")),
    (GroupBy.BUSINESS_UNIT, ("business unit", "bu", "বিজনেস ইউনিট")),
    (GroupBy.SALES_LINE, ("sales line", "salesline", "সেলস লাইন")),
    (GroupBy.REGION, ("region", "regions", "রিজিওন", "অঞ্চল")),
    (GroupBy.ZONE, ("zone", "zones", "জোন")),
    (GroupBy.AREA, ("area", "areas", "এরিয়া")),
    (GroupBy.UNIT, ("unit", "units", "ইউনিট")),
    # The item nouns, all three levels answered from the Material Master since
    # revision 0022. Before then there were six — SKU, brand and category from
    # the sales master, material, material brand and material group from the
    # plant's — and the vocabulary had to keep them apart because they named
    # different columns on different views that could not be joined. One master
    # means one set of nouns: "brand-wise sales" and "stock by brand" now group
    # the same goods by the same column.
    #
    # **Order is the whole design here**, because the loop consumes the phrase it
    # matches. Three constraints, and they are why the brand level appears twice:
    #
    # 1. "material group" and "material brand" must be consumed before the bare
    #    "material", or a material-group question is answered material by
    #    material.
    # 2. "sku code", "product code" and "product name" must be consumed before
    #    the bare "product", or an explicitly item-level question is answered
    #    brand by brand.
    # 3. A *general* item question is a brand question in this business. "Top
    #    products", "best performing products" and "প্রোডাক্ট-wise sales" are
    #    asked when someone wants to know which of the company's lines are
    #    selling, and an answer listing fifteen pack sizes of one brand is not
    #    that answer. So the bare nouns land on the brand entry — the second one,
    #    after rule 2 has taken its share.
    (GroupBy.MATERIAL_GROUP, ("material group", "material groups", "matl group",
                              "product group", "product groups",
                              "category", "categories",
                              "ম্যাটেরিয়াল গ্রুপ", "ক্যাটাগরি")),
    (GroupBy.MATERIAL_BRAND, ("material brand", "material brands", "matl brand",
                              "ম্যাটেরিয়াল ব্র্যান্ড")),
    (GroupBy.MATERIAL, ("material code", "material codes", "material no",
                        "material number", "material description",
                        "sku code", "sku codes", "product code", "product codes",
                        "product name", "product names", "sku", "skus",
                        "item", "items", "material", "materials",
                        "এসকেইউ", "ম্যাটেরিয়াল")),
    (GroupBy.MATERIAL_BRAND, ("brand", "brands", "ব্র্যান্ড",
                              "product", "products", "প্রোডাক্ট", "পণ্য")),
    (GroupBy.CUSTOMER, ("customer", "customers", "dealer", "dealers", "party",
                        "কাস্টমার", "গ্রাহক")),
    # Where a stock position is held. "storage location" is listed before
    # "plant" so the two-word phrase is consumed first and never leaves a stray
    # "plant".
    (GroupBy.STORAGE_LOCATION, ("storage location", "storage locations",
                                "storage loc", "sloc", "স্টোরেজ লোকেশন")),
    (GroupBy.PLANT, ("plant", "plants", "প্ল্যান্ট")),
    (GroupBy.COMPANY, ("company", "companies")),
    (GroupBy.MONTH, ("month", "monthly", "মাস")),
    (GroupBy.DATE, ("day", "daily", "দিন")),
)

#: Phrases that turn a dimension noun into a *grouping* request.
#:
#: This distinction matters: "Dhaka region-এর sales" names a region to filter
#: by, while "region-wise sales" asks for a breakdown. Without a marker the
#: noun is treated as a qualifier, not a grouping.
GROUPING_MARKERS: tuple[str, ...] = (
    r"{noun}s?\s*[-–]?\s*wise",       # region-wise, region wise, products wise
    r"\bby\s+{noun}s?\b",             # sales by territory
    r"\bper\s+{noun}s?\b",            # sales per region
    r"\beach\s+{noun}s?\b",           # each region
    r"{noun}\s*ভিত্তিক",               # অঞ্চলভিত্তিক
    r"{noun}\s*অনুযায়ী",               # রিজিওন অনুযায়ী
    r"\b{noun}s\b",                   # plural: "top 10 products"
    # "top SKU", "top 15 brand", "শীর্ষ ১০ brand" — a ranking request names the
    # dimension it wants ranked, so the singular counts here even though a bare
    # singular noun elsewhere is only a filter.
    r"\b(?:top|bottom|highest|lowest|শীর্ষ)\s*\d*\s*{noun}s?\b",
)

#: "top 20", "শীর্ষ ২০", "show 50"
LIMIT_RE = re.compile(r"\b(?:top|first|show|highest|lowest|শীর্ষ)\s*(\d{1,3})\b")
#: "below 80%", "less than 15 days", "৮০% এর নিচে"
THRESHOLD_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
DAYS_RE = re.compile(r"(\d{1,4})\s*(?:days?|দিন)")


@dataclass
class IntentPrediction:
    intent: Intent
    language: Language
    metric: str | None = None
    modifiers: set[str] = field(default_factory=set)
    group_by: list[GroupBy] = field(default_factory=list)
    limit: int | None = None
    percent_threshold: float | None = None
    days_threshold: int | None = None
    #: The stock category a stock question named, or ``None`` for the total.
    stock_measure: str | None = None
    confidence: float = 0.0

    def has(self, modifier: str) -> bool:
        return modifier in self.modifiers


def _with_learned(shipped: dict[str, tuple[str, ...]],
                  learned: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """The shipped keyword table, widened by approved aliases.

    A copy, never a mutation: these dictionaries are module-level and shared by
    every request, so widening one in place would leak one deployment's
    vocabulary into every future call and make the classifier depend on
    whichever question ran first.

    An approved phrase is *appended* to its target's keyword list, so where a
    shipped keyword and a learned one both match the earliest mention still
    wins — the same rule that already decided between two shipped keywords.
    """
    if not learned:
        return shipped
    widened = {key: list(values) for key, values in shipped.items()}
    for phrase, target in learned.items():
        if target in widened and phrase not in widened[target]:
            widened[target].append(phrase)
    return {key: tuple(values) for key, values in widened.items()}


def _find_metric(text: str, learned: dict[str, str] | None = None) -> str | None:
    """The metric family with the earliest mention wins ties naturally."""
    best: tuple[int, str] | None = None
    for metric, keywords in _with_learned(METRIC_KEYWORDS, learned or {}).items():
        for keyword in keywords:
            position = text.find(keyword)
            if position >= 0 and (best is None or position < best[0]):
                best = (position, metric)
    return best[1] if best else None


def _find_modifiers(text: str, learned: dict[str, str] | None = None) -> set[str]:
    found = set()
    for modifier, keywords in _with_learned(MODIFIERS, learned or {}).items():
        if any(keyword in text for keyword in keywords):
            found.add(modifier)
    return found


def _names_a_unit(text: str) -> bool:
    """Whether the question mentions a unit of measure at all.

    Used for routing only. Naming a unit says the asker means volume rather
    than taka — "July মাসে কত LTR sales হয়েছে?" is a volume question — but it
    cannot narrow the answer: a transaction line records one Total Volume and
    no unit, so there is no KG subset of it to return.
    """
    return any(
        re.search(rf"\b{re.escape(word)}\b", text)
        for _, keywords in VOLUME_UNIT_KEYWORDS
        for word in keywords
    )


def _group_nouns(learned: dict[str, str] | None,
                 ) -> tuple[tuple[GroupBy, tuple[str, ...]], ...]:
    """``GROUP_BY_KEYWORDS`` widened by approved aliases, order preserved.

    Order is load-bearing here in a way it is not for metrics: the table is
    searched top-down and a matched phrase is consumed, which is what stops
    "sub territory" being re-matched as "territory". A learned noun joins its
    own group's list and changes nothing about that ordering.
    """
    if not learned:
        return GROUP_BY_KEYWORDS
    extra: dict[str, list[str]] = {}
    for phrase, target in learned.items():
        extra.setdefault(target, []).append(phrase)
    return tuple(
        (group, tuple(nouns) + tuple(
            phrase for phrase in extra.get(group.value, [])
            if phrase not in nouns
        ))
        for group, nouns in GROUP_BY_KEYWORDS
    )


def _find_group_by(text: str,
                   learned: dict[str, str] | None = None) -> list[GroupBy]:
    """Grouping dimensions explicitly requested by the question.

    A dimension only counts when it carries a grouping marker ("-wise", "by X",
    a plural, "ভিত্তিক"). A bare noun — as in "Dhaka region-এর sales" — is left
    alone so it stays a filter rather than becoming a breakdown. A learned noun
    is held to the same rule: teaching a word does not make it a breakdown, it
    makes it a word the existing rules can recognise.
    """
    found: list[GroupBy] = []
    consumed = text
    for group, nouns in _group_nouns(learned):
        matched = False
        for noun in nouns:
            escaped = re.escape(noun)
            for marker in GROUPING_MARKERS:
                pattern = marker.format(noun=escaped)
                match = re.search(pattern, consumed, re.IGNORECASE)
                if match:
                    if group not in found:
                        found.append(group)
                    # Consume the phrase so a broader noun cannot re-match it.
                    consumed = consumed[:match.start()] + " " + consumed[match.end():]
                    matched = True
                    break
            if matched:
                break
    return found


def detect_intent(message: str,
                  lexicon: Lexicon | None = None) -> IntentPrediction:
    """Classify a question into a business intent.

    ``lexicon`` carries the phrases a reviewer has approved. It is optional and
    defaults to none, so this stays a pure function of its input for every
    caller that has no session — a script, a test of the classifier itself —
    and the shipped keyword tables remain the whole vocabulary unless somebody
    has deliberately widened them.
    """
    language = detect_language(message)
    text = normalize_digits(message).lower()

    learned = lexicon or LEXICON_EMPTY
    metric = _find_metric(text, learned.metrics)
    modifiers = _find_modifiers(text, learned.modifiers)
    group_by = _find_group_by(text, learned.group_by)

    # Naming a unit asks a volume question even without the word "volume":
    # "July মাসে কত LTR sales হয়েছে?" is about volume, not about taka.
    if _names_a_unit(text):
        modifiers.add("volume")

    limit_match = LIMIT_RE.search(text)
    limit = int(limit_match.group(1)) if limit_match else None
    percent_match = THRESHOLD_PERCENT_RE.search(text)
    percent = float(percent_match.group(1)) if percent_match else None
    days_match = DAYS_RE.search(text)
    days = int(days_match.group(1)) if days_match else None

    intent = _classify(text, metric, modifiers, group_by)
    confidence = 0.0
    if intent is not Intent.UNKNOWN:
        confidence = 0.9 if metric or "summary" in modifiers or "why" in modifiers else 0.6

    # Only for a stock question. "Blocked" is a stock category; anywhere else
    # the word means nothing to this system, and reading it as one would attach
    # a stock ranking to a sales answer.
    stock_measure = _find_stock_measure(text) if metric == "stock" else None

    return IntentPrediction(
        intent=intent,
        language=language,
        metric=metric,
        modifiers=modifiers,
        group_by=group_by,
        limit=limit,
        percent_threshold=percent,
        days_threshold=days,
        stock_measure=stock_measure,
        confidence=confidence,
    )


def _find_stock_measure(text: str) -> str | None:
    """Which of the four stock categories the question named, if any."""
    for measure, keywords in STOCK_MEASURE_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return measure
    return None


def _classify(text: str, metric: str | None, modifiers: set[str],
              group_by: list[GroupBy]) -> Intent:
    """Map (metric, modifiers, grouping) onto one intent."""
    # Cross-cutting intents first: they are about the business, not one metric.
    if "why" in modifiers:
        return Intent.ROOT_CAUSE_ANALYSIS
    if "alert" in modifiers:
        return Intent.BUSINESS_ALERT
    # Volume before the per-metric branches, but never for stock: material
    # stock is counted in the four category quantities and carries no volume
    # at all, so "stock volume" must fall through to the stock branch rather
    # than be answered with a *sales* volume, which is a different measure.
    if "volume" in modifiers and metric != "stock":
        return Intent.SALES_VOLUME
    if "summary" in modifiers and metric is None:
        return Intent.BUSINESS_SUMMARY
    if "summary" in modifiers and "management" in text:
        return Intent.BUSINESS_SUMMARY
    if "bottom" in modifiers and metric is None:
        return Intent.ROOT_CAUSE_ANALYSIS

    if metric == "stock":
        # Expiry first: "which locations have stock expiring soon" is an expiry
        # question that happens to name a location, not a location breakdown.
        if "expiring" in modifiers:
            return Intent.EXPIRING_STOCK
        if "expiry" in modifiers:
            return Intent.STOCK_EXPIRY
        if GroupBy.PLANT in group_by:
            return Intent.STOCK_BY_PLANT
        if GroupBy.STORAGE_LOCATION in group_by:
            return Intent.STOCK_BY_STORAGE_LOCATION
        if GroupBy.MATERIAL_GROUP in group_by:
            return Intent.STOCK_BY_MATERIAL_GROUP
        # A brand asked of stock is the material brand, and since revision 0022
        # there is no other kind — the qualifier the vocabulary used to need is
        # gone with the second master.
        if GroupBy.MATERIAL_BRAND in group_by:
            return Intent.STOCK_BY_MATERIAL_BRAND
        # After the group and the brand, never before them: "material group
        # wise" names both nouns, and the group is the one the question asked
        # for.
        if GroupBy.MATERIAL in group_by:
            return Intent.STOCK_BY_MATERIAL
        return Intent.STOCK_SUMMARY

    if metric == "target":
        if "gap" in modifiers:
            return Intent.TARGET_GAP
        if "achievement" in text or "achieve" in text or "অর্জন" in text or group_by:
            return Intent.TARGET_ACHIEVEMENT
        return Intent.TARGET_SUMMARY

    if metric == "sales" or metric is None:
        if "trend" in modifiers:
            return Intent.SALES_TREND
        if "growth" in modifiers:
            return Intent.SALES_GROWTH
        if "achievement" in text or "achieve" in text or "অর্জন" in text:
            return Intent.SALES_ACHIEVEMENT
        if group_by:
            return _performance_intent(group_by[0])
        if metric == "sales":
            return Intent.SALES_SUMMARY

    return Intent.UNKNOWN


#: Grouped questions map to the matching performance intent.
_PERFORMANCE_BY_GROUP: dict[GroupBy, Intent] = {
    GroupBy.REGION: Intent.REGION_PERFORMANCE,
    GroupBy.ZONE: Intent.ZONE_PERFORMANCE,
    GroupBy.AREA: Intent.AREA_PERFORMANCE,
    GroupBy.UNIT: Intent.UNIT_PERFORMANCE,
    GroupBy.TERRITORY: Intent.TERRITORY_PERFORMANCE,
    GroupBy.SUB_TERRITORY: Intent.SUB_TERRITORY_PERFORMANCE,
    GroupBy.MATERIAL: Intent.MATERIAL_PERFORMANCE,
    GroupBy.MATERIAL_GROUP: Intent.MATERIAL_GROUP_PERFORMANCE,
    GroupBy.MATERIAL_BRAND: Intent.MATERIAL_BRAND_PERFORMANCE,
    GroupBy.CUSTOMER: Intent.CUSTOMER_PERFORMANCE,
    GroupBy.SALES_FORCE: Intent.SALES_FORCE_PERFORMANCE,
}


def _performance_intent(group: GroupBy) -> Intent:
    return _PERFORMANCE_BY_GROUP.get(group, Intent.SALES_DETAIL)


__all__ = [
    "Intent",
    "IntentPrediction",
    "detect_intent",
    "detect_language",
    "METRIC_KEYWORDS",
    "MODIFIERS",
    "GROUP_BY_KEYWORDS",
]
