"""The orchestration pipeline.

    message
      -> injection sanitising
      -> language + intent detection
      -> entity resolution (official master data only)
      -> date resolution (configured financial year)
      -> conversation context merge (follow-up questions)
      -> permission check            <- before any query
      -> tool selection (LLM when configured, deterministic planner otherwise)
      -> tool execution              <- scope injected into the query
      -> result validation
      -> response formatting
      -> answer

Every step is separately testable, and the LLM participates in exactly two of
them: choosing the tool and phrasing the prose. It cannot reach the database,
alter a filter, or introduce a number that is not in validated tool output.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from sqlalchemy.orm import Session

from ..etl.mapping import MasterDataIndex
from .date_resolver import MONTH_NAMES, DateResolver
from .entity_resolver import EntityResolver
from .exceptions import (
    AgentError,
    AmbiguousEntityError,
    DateResolutionError,
    NoDataError,
    PermissionDeniedError,
    UnsupportedQuestionError,
    ValidationFailedError,
)
from . import lexicon, masters
from .intent import (
    GROUP_BY_KEYWORDS,
    METRIC_KEYWORDS,
    MODIFIERS,
    IntentPrediction,
    detect_intent,
)
from .mining import normalize_phrase
from .llm import LLMClient, NullLLMClient
from .permission_filter import PermissionFilter, UserContext
from .prompts import (
    ANSWER_PROMPT,
    INJECTION_REFUSAL,
    PLANNER_PROMPT,
    build_system_prompt,
    conversation_messages,
    planner_examples,
    sanitize_message,
)
from .response_formatter import ResponseFormatter
from .schemas import (
    CHAT_CONTEXT_LEVELS,
    ChartSpec,
    ChatContext,
    DateRangeType,
    EntityType,
    GroupBy,
    ORG_ENTITY_TYPES,
    Intent,
    ResolvedDateRange,
    ResolvedEntity,
    ScopeFilters,
    StructuredQuery,
    ToolResult,
)
from .tools import REGISTRY, ToolContext, ToolInvocation, execute_tool, tools_for_intent
from .validators import numbers_in, response_grounded_in, validate_tool_result

logger = logging.getLogger("app.ai.orchestrator")

#: Intent -> the tool that answers it deterministically.
TOOL_BY_INTENT: dict[Intent, str] = {
    Intent.SALES_SUMMARY: "get_sales_summary",
    Intent.SALES_DETAIL: "get_sales_detail",
    Intent.SALES_TREND: "get_sales_trend",
    Intent.SALES_GROWTH: "get_sales_growth",
    Intent.SALES_TARGET: "get_sales_target",
    Intent.SALES_ACHIEVEMENT: "get_sales_achievement",
    Intent.SALES_VOLUME: "get_sales_volume",
    Intent.STOCK_SUMMARY: "get_stock_summary",
    Intent.STOCK_BY_PLANT: "get_stock_by_plant",
    Intent.STOCK_BY_STORAGE_LOCATION: "get_stock_by_storage_location",
    Intent.STOCK_BY_MATERIAL: "get_stock_by_material",
    Intent.STOCK_BY_MATERIAL_GROUP: "get_stock_by_material_group",
    Intent.STOCK_BY_MATERIAL_BRAND: "get_stock_by_material_brand",
    Intent.STOCK_EXPIRY: "get_stock_expiry",
    Intent.EXPIRING_STOCK: "get_expiring_stock",
    Intent.TARGET_SUMMARY: "get_target_summary",
    Intent.TARGET_ACHIEVEMENT: "get_target_achievement",
    Intent.TARGET_GAP: "get_target_gap",
    Intent.REGION_PERFORMANCE: "get_region_performance",
    Intent.ZONE_PERFORMANCE: "get_zone_performance",
    Intent.AREA_PERFORMANCE: "get_area_performance",
    Intent.UNIT_PERFORMANCE: "get_unit_performance",
    Intent.TERRITORY_PERFORMANCE: "get_territory_performance",
    Intent.SUB_TERRITORY_PERFORMANCE: "get_sub_territory_performance",
    Intent.MATERIAL_PERFORMANCE: "get_material_performance",
    Intent.MATERIAL_BRAND_PERFORMANCE: "get_material_brand_performance",
    Intent.MATERIAL_GROUP_PERFORMANCE: "get_material_group_performance",
    Intent.CUSTOMER_PERFORMANCE: "get_customer_performance",
    Intent.SALES_FORCE_PERFORMANCE: "get_salesforce_performance",
    Intent.BUSINESS_SUMMARY: "get_business_summary",
    Intent.BUSINESS_ALERT: "get_business_alerts",
    Intent.ROOT_CAUSE_ANALYSIS: "get_root_cause_analysis",
    # Receivables. The three tools existed, were tested and were documented as
    # answerable, but no intent named one — so every credit question reached
    # `_select_tool`, found nothing here, and was refused as unsupported. The
    # tools were never the missing part; this line was.
    Intent.CREDIT_SUMMARY: "get_credit_summary",
    Intent.CREDIT_AGING: "get_credit_aging",
    Intent.CREDIT_OVERDUE: "get_overdue_customers",
}

#: The organisational chain, outermost first, as entity types.
#:
#: Derived from the levels the warehouse is built on rather than restated, so
#: this cannot drift from the hierarchy the ETL and the filter bar agree on.
ORG_ORDER: tuple[EntityType, ...] = tuple(
    CHAT_CONTEXT_LEVELS[name] for name in
    ("company_code", "bu_code", "sales_line_code", "zone_code", "region_code",
     "area_code", "unit_code", "territory_code", "sub_territory_code")
)

#: Rows a ranked answer shows when nobody said how many.
#:
#: A conversation carries a limit forward only while it *differs* from this, so
#: the platform's own default is never repeated back to a reader as though it
#: were their decision. A reader who does say "top 20" loses nothing: the
#: fallback is the number they asked for.
DEFAULT_LIMIT = 20

#: Where an entity type sits when two of them are compared, coarsest first.
#:
#: An axis is a chain of narrowings over one thing, so two entities on the same
#: axis can contradict each other and two on different axes never can: a region
#: and a territory are rival answers to "where", while a territory and a
#: material are two halves of one question.
#:
#: Customer and sales force share the rank below the organisational levels.
#: They are two different things a territory holds — a shop and a person — so
#: neither displaces the other, while a level above displaces both.
_ORG_AXIS: dict[EntityType, int] = {
    **{entity_type: rank for rank, entity_type in enumerate(ORG_ENTITY_TYPES)},
    EntityType.CUSTOMER: len(ORG_ENTITY_TYPES),
    EntityType.SALES_FORCE: len(ORG_ENTITY_TYPES),
}

#: The item axis. One entry today, because revision 0022 left one item master;
#: a material group or brand entity would join it here rather than anywhere else.
_ITEM_AXIS: dict[EntityType, int] = {EntityType.MATERIAL: 0}

ENTITY_AXES: tuple[dict[EntityType, int], ...] = (_ORG_AXIS, _ITEM_AXIS)


#: Intents whose answer is more useful with a companion figure alongside.
COMPANION_TOOLS: dict[Intent, tuple[str, ...]] = {
    Intent.SALES_SUMMARY: ("get_sales_growth", "get_sales_achievement"),
}

#: Entity type -> the ``ScopeFilters`` list it populates.
FILTER_FIELD_BY_ENTITY: dict[EntityType, str] = {
    EntityType.COMPANY: "company_codes",
    EntityType.BUSINESS_UNIT: "business_unit_codes",
    EntityType.SALES_LINE: "sales_line_codes",
    EntityType.ZONE: "zone_codes",
    EntityType.REGION: "region_codes",
    EntityType.AREA: "area_codes",
    EntityType.UNIT: "unit_codes",
    EntityType.TERRITORY: "territory_codes",
    EntityType.SUB_TERRITORY: "sub_territory_codes",
    EntityType.CUSTOMER: "customer_codes",
    EntityType.SALES_FORCE: "sales_force_codes",
    EntityType.MATERIAL: "material_codes",
}

#: Tools whose input schema accepts each optional argument.
_GROUPED_TOOLS = {
    "get_sales_detail",
    *(name for name in REGISTRY if name.endswith("_performance")),
}
_ACHIEVEMENT_TOOLS = {
    "get_sales_target", "get_sales_achievement", "get_target_achievement",
    "get_target_gap",
}
_STOCK_TOOLS = {
    "get_stock_summary", "get_stock_by_plant", "get_stock_by_storage_location",
    "get_stock_by_material", "get_stock_by_material_group", "get_stock_expiry",
    "get_expiring_stock",
}

#: The intents a stock tool answers — derived from the map above rather than
#: listed again, so a stock intent added there cannot be forgotten here.
#:
#: Nothing in this module reads it since revision 0022 removed the material
#: entity-type exclusion it was written for. It stays because it is *derived*:
#: it cannot go stale, and "which intents are stock intents" is a question the
#: next follow-up rule will ask again.
STOCK_INTENTS: frozenset[Intent] = frozenset(
    intent for intent, tool in TOOL_BY_INTENT.items() if tool in _STOCK_TOOLS
)
_GROWTH_TOOLS = {"get_sales_growth"}
_VOLUME_TOOLS = {"get_sales_volume"}
_TREND_TOOLS = {"get_sales_trend"}


@dataclass
class ConversationContext:
    """What a follow-up question inherits from the turn before it."""

    intent: Intent | None = None
    date_range: ResolvedDateRange | None = None
    entities: list[ResolvedEntity] = field(default_factory=list)
    group_by: list[GroupBy] = field(default_factory=list)
    limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value if self.intent else None,
            "date_range": (
                self.date_range.model_dump(mode="json") if self.date_range else None
            ),
            "entities": [e.model_dump(mode="json") for e in self.entities],
            "group_by": [g.value for g in self.group_by],
            "limit": self.limit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ConversationContext":
        if not data:
            return cls()
        try:
            return cls(
                intent=Intent(data["intent"]) if data.get("intent") else None,
                date_range=(
                    ResolvedDateRange.model_validate(data["date_range"])
                    if data.get("date_range") else None
                ),
                entities=[ResolvedEntity.model_validate(e)
                          for e in data.get("entities") or []],
                group_by=[GroupBy(g) for g in data.get("group_by") or []],
                limit=data.get("limit"),
            )
        except Exception:  # noqa: BLE001 - stale context is dropped, never fatal
            logger.debug("discarding unreadable conversation context")
            return cls()


@dataclass
class AgentAnswer:
    """The orchestrator's complete output for one question."""

    answer: str
    intent: Intent
    query: StructuredQuery | None = None
    results: list[ToolResult] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)
    context: ConversationContext = field(default_factory=ConversationContext)
    needs_clarification: bool = False
    error_code: str | None = None
    #: The failed exception's own ``details``, carried rather than discarded.
    #:
    #: ``EntityNotFoundError`` knows *which term* it could not resolve, and that
    #: term is the whole value of the signal: "something failed" is noise, while
    #: "nobody could look up 'chini'" is a phrase somebody can teach. The field
    #: never reaches ``ChatResponse`` — it exists so ``ai.mining`` can record
    #: what failed, not so a client can read it.
    error_details: dict[str, Any] = field(default_factory=dict)
    language: str = "en"
    injection_detected: bool = False
    elapsed_ms: int = 0

    @property
    def chart(self) -> ChartSpec | None:
        for result in self.results:
            if result.chart and result.chart.data:
                return result.chart
        return None

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for result in self.results:
            for source in result.sources:
                if source not in seen:
                    seen.append(source)
        return seen

    @property
    def tools_used(self) -> list[str]:
        return [i.tool_name for i in self.invocations]


class Orchestrator:
    """Turns a question into a grounded answer."""

    def __init__(self, session: Session, user: UserContext,
                 llm: LLMClient | None = None, today: dt.date | None = None) -> None:
        self.session = session
        self.user = user
        self.llm = llm or NullLLMClient()
        self.today = today or dt.date.today()
        # Both master indexes are cached on a generation counter rather than
        # rebuilt here: an Orchestrator is constructed per question, and reading
        # every dimension again each time was twenty-eight of the forty-three
        # statements one answer cost. See ``ai.masters``.
        self.master_index = masters.master_index(session)
        self.permissions = PermissionFilter(session, user, self.master_index)
        # Loaded once per question and handed to both readers, so the classifier
        # and the entity resolver are always looking at the same approved
        # vocabulary. Cached on a generation counter, so this is a dictionary
        # lookup on all but the first question after a reviewer's change.
        self.lexicon = lexicon.load(session)
        self.entities = EntityResolver(session, index=masters.entity_index(session),
                                       lexicon=self.lexicon)
        self.dates = DateResolver(today=self.today)
        self.formatter = ResponseFormatter()

    # -- pipeline -----------------------------------------------------------

    def answer(self, message: str, context: ConversationContext | None = None,
               history: Sequence[dict[str, str]] = (),
               page: ChatContext | None = None) -> AgentAnswer:
        started = time.perf_counter()
        context = context or ConversationContext()

        cleaned, injections = sanitize_message(message)
        prediction = detect_intent(cleaned or message, self.lexicon)
        prediction = self._apply_example(cleaned or message, prediction)

        if injections and not cleaned:
            # Nothing left once the injection is removed: there was no question.
            return self._finish(AgentAnswer(
                answer=INJECTION_REFUSAL, intent=Intent.UNKNOWN,
                language=prediction.language, injection_detected=True,
                error_code="PROMPT_INJECTION",
            ), started)

        try:
            query = self.build_query(cleaned or message, prediction, context, page)
            answer = self._execute(query, cleaned or message, history)
        except AgentError as exc:
            return self._finish(AgentAnswer(
                answer=exc.user_message,
                intent=prediction.intent,
                language=prediction.language,
                needs_clarification=isinstance(exc, AmbiguousEntityError),
                error_code=exc.code,
                error_details=exc.details,
                injection_detected=bool(injections),
                context=context,
            ), started)

        answer.injection_detected = bool(injections)
        answer.language = prediction.language
        return self._finish(answer, started)

    @staticmethod
    def _finish(answer: AgentAnswer, started: float) -> AgentAnswer:
        answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return answer

    # -- planning -----------------------------------------------------------

    @staticmethod
    def _excluded_entity_types(prediction: IntentPrediction,
                               context: ConversationContext) -> set[EntityType]:
        """Entity types this question cannot be naming. There are none left.

        Materials used to be excluded from every non-stock question, because a
        material meant stock and nothing else: a material filter on a sales
        report would have narrowed it by a column the sales view did not carry,
        and a material code is often a bare number, so the "20" in "top 20
        customers" could attach one by accident.

        Revision 0022 made the Material Master the *only* item master. A sale and
        a target both name a material now, so excluding the type here would make
        "Rugby sales" unanswerable — the one item filter the agent has would be
        unreachable from the questions it exists to answer.

        The bare-number risk is handled where it actually arises rather than by
        withholding the whole type: a ranking phrase like "top 20" is consumed by
        intent detection before entity resolution runs, and what survives only
        matches a material if the master literally holds that code — in which
        case it is a real ambiguity and asking is the right answer.
        """
        return set()

    @staticmethod
    def _axis_of(entity_type: EntityType) -> dict[EntityType, int] | None:
        """The axis an entity type is compared on, or None if it stands alone."""
        for axis in ENTITY_AXES:
            if entity_type in axis:
                return axis
        return None

    @classmethod
    def _carry_entities(cls, named: Sequence[ResolvedEntity],
                        earlier: Sequence[ResolvedEntity],
                        ) -> tuple[list[ResolvedEntity], list[ResolvedEntity]]:
        """Which earlier filters a follow-up leaves standing, and which it ends.

        A follow-up that names something used to discard **every** earlier
        filter, which turned "A M Traders এর টা দেখাও" after a territory
        question into that customer's national figure — a reader asking to
        narrow, answered wider, with nothing on screen saying so.

        The rule is the one the filter bar already follows: an earlier filter
        ends where the follow-up names its own type, or names something *above*
        it on the same axis. Naming a region after a territory is stepping out,
        so the territory goes; naming a customer inside that territory is
        drilling in, so the territory stays. A material and a territory are
        never rivals and both stand.

        Both halves are returned because both have to be said out loud: a filter
        that silently vanished and one that silently persisted mislead a reader
        in opposite directions.
        """
        kept: list[ResolvedEntity] = []
        ended: list[ResolvedEntity] = []
        for old in earlier:
            axis = cls._axis_of(old.entity_type)
            displaced = any(
                new.entity_type is old.entity_type
                or (axis is not None and new.entity_type in axis
                    and axis[old.entity_type] > axis[new.entity_type])
                for new in named
            )
            (ended if displaced else kept).append(old)
        return kept, ended

    def _page_entities(self, page: "ChatContext | None",
                       already: Sequence[ResolvedEntity]) -> list[ResolvedEntity]:
        """The bar's selections, for levels the question did not name itself.

        Each is looked up in the master index rather than trusted as typed, so a
        code the browser sent that names nothing is dropped instead of becoming
        a filter that silently matches no rows. They are ordinary entities from
        here on, and pass the same permission check the question's own do.
        """
        if page is None:
            return []
        taken = {entity.entity_type for entity in already}

        # How deep into the organisation the question itself went. A bar
        # selection *below* that level is superseded: a reader looking at one
        # territory who then asks about another region has moved, and ANDing the
        # two would answer with an empty table for a question that has an
        # answer. Levels above stay — a company and a region inside it are two
        # things the reader has both expressed.
        named = [ORG_ORDER.index(e.entity_type) for e in already
                 if e.entity_type in ORG_ORDER]
        deepest_named = max(named) if named else None

        inherited: list[ResolvedEntity] = []
        for field_name, entity_type in CHAT_CONTEXT_LEVELS.items():
            code = getattr(page, field_name, None)
            if not code or entity_type in taken:
                continue
            if (deepest_named is not None and entity_type in ORG_ORDER
                    and ORG_ORDER.index(entity_type) > deepest_named):
                continue
            entry = next(
                (e for e in self.entities.index.by_code.get(code.casefold(), [])
                 if e.entity_type is entity_type),
                None,
            )
            if entry is None:
                continue
            inherited.append(ResolvedEntity(
                entity_type=entry.entity_type, code=entry.code,
                label=entry.label, term=code, match="code",
            ))
        return inherited

    def _page_period(self, page: "ChatContext | None") -> ResolvedDateRange | None:
        """The bar's period, resolved the way the dashboard resolves it.

        Consulted last — after the question's own words and after the period the
        conversation was already using — so it fills a gap and never overrules
        something the reader said.
        """
        if page is None:
            return None
        if page.date_from and page.date_to:
            return self.dates.custom_range(page.date_from, page.date_to)
        if not page.period:
            return None
        try:
            return self.dates.of_type(DateRangeType(page.period.upper()))
        except (ValueError, KeyError, DateResolutionError):
            # A period name this resolver does not know is not worth failing a
            # question over: the ordinary fallback still applies, and it says so.
            return None

    @staticmethod
    def _known_words() -> set[str]:
        """Every word the question layer already accounts for.

        Built from the tables that define them — the metric families, the
        modifiers, the grouping nouns and the month names — rather than typed
        out again here. A second list would fall behind the first, and the
        symptom would be the assistant reporting "sales" as a master record it
        could not find.
        """
        words: set[str] = set()
        for group in METRIC_KEYWORDS.values():
            words.update(w.casefold() for w in group)
        for group in MODIFIERS.values():
            words.update(w.casefold() for w in group)
        for _, group in GROUP_BY_KEYWORDS:
            words.update(w.casefold() for w in group)
        words.update(MONTH_NAMES)
        # Multi-word phrases contribute their parts too, because the check that
        # reads this compares one token at a time.
        for phrase in list(words):
            words.update(phrase.split())
        return words

    def build_query(self, message: str, prediction: IntentPrediction,
                    context: ConversationContext,
                    page: "ChatContext | None" = None) -> StructuredQuery:
        """Assemble and validate the structured query for a message."""
        assumptions: list[str] = []

        named = self.entities.resolve_message(
            message, exclude_types=self._excluded_entity_types(prediction, context))
        # A word already read as a period is not also a guess at a master name.
        #
        # "dec" is December, and it is also the opening of "Decoquinate 6%
        # -Zamiquin 25kg (1's)" — so "24-25 year এর dec মাসের region wise sales"
        # was answered for that one material and came back empty, having been
        # read as a date *and* as an item at the same time. The same trap is set
        # by "mar", "may" and "jun", each of which opens some material's name.
        #
        # Only a **partial** match is refused. A partial name is the weakest
        # evidence this resolver acts on, and a period word is a use of that
        # word the question has already accounted for. An exact code or an exact
        # name still wins: a material genuinely called "May" is found by being
        # named, not by sharing three letters with something.
        named = [
            entity for entity in named
            if not (entity.match == "partial_name"
                    and self.dates.is_period_word(entity.term))
        ]
        carried, ended = self._carry_entities(named, context.entities)
        entities = named + carried

        # A word that reads like a master name and matched nothing has to be
        # said out loud. Without this the question is answered for everything —
        # a national total where a territory was asked for — and the two answers
        # look identical on screen. Naming the word is not a guess: it reports
        # what was read, and leaves the reader to correct it.
        unmatched = [
            term for term in self.entities.unmatched_terms(
                message, named, known_words=self._known_words())
            if not self.dates.is_period_word(term)
        ]
        if unmatched:
            # A near miss is offered, never taken. Every name here is a record
            # that exists, so the reader can correct one keystroke instead of
            # rewriting the question — and choosing one of them for them would
            # be this system deciding which territory was meant.
            suggestions = [
                f"'{term}' (did you mean {near[0].label}?)" if (near := self.entities.suggest(term))
                else f"'{term}'"
                for term in unmatched
            ]
            assumptions.append(
                "No master record matches " + ", ".join(suggestions)
                + ", so nothing was filtered by it."
            )

        # What the reader has on screen narrows the answer too, for the levels
        # the question did not name itself. The question wins its own level —
        # "Khulna sales" with the bar on Dhaka is a question about Khulna — and
        # the rest is inherited and said out loud, because a filter nobody can
        # see is the same problem as a filter that silently vanished.
        inherited_filters = self._page_entities(page, entities)
        if inherited_filters:
            entities = entities + inherited_filters
            assumptions.append(
                "Also filtered by what is selected on screen: "
                + ", ".join(e.label for e in inherited_filters) + "."
            )

        intent = prediction.intent
        if intent is Intent.UNKNOWN and context.intent:
            intent = context.intent
            assumptions.append(
                f"Continuing from your previous question about "
                f"{context.intent.value.replace('_', ' ').lower()}."
            )
        if intent is Intent.UNKNOWN:
            raise UnsupportedQuestionError()

        # A follow-up that only changes the grouping keeps the earlier metric.
        if (prediction.group_by and context.intent
                and prediction.metric is None
                and intent in (Intent.SALES_DETAIL, Intent.REGION_PERFORMANCE)):
            intent = self._regroup_intent(context.intent, prediction.group_by[0]) or intent

        detected_period = self.dates.detect(message)
        if detected_period is not None:
            # resolve_with_comparison keeps "compared to last month" meaning
            # *this* month measured against last month.
            date_range = self.dates.resolve_with_comparison(message)
            # Read on its own, "February দেখাও" means the most recent February.
            # Asked after a question about January of FY 2024-25 it means that
            # year's February, and answering with a February thirteen months
            # away — while labelling it only "February" — is the silent kind of
            # wrong this platform exists to refuse. The anchoring is stated,
            # because a period that moved is a period the reader must see move.
            anchored = (
                self.dates.anchor_month(message, context.date_range)
                if context.date_range is not None else None
            )
            if anchored is not None and anchored.date_from != date_range.date_from:
                if anchored.date_from <= self.dates.today:
                    assumptions.append(
                        f"Read as {anchored.label}, the same financial year as "
                        f"your previous question ({context.date_range.label})."
                    )
                    date_range = anchored
                else:
                    # The year under discussion has not reached that month.
                    # Anchoring anyway would answer with an empty future period,
                    # which trades one wrong figure for another — so the month is
                    # read on its own and the reader is told the answer has left
                    # the year they were asking about. Either way the period
                    # never moves without being named.
                    assumptions.append(
                        f"{anchored.label} has not begun, so this covers "
                        f"{date_range.label} — outside the financial year your "
                        f"previous question covered ({context.date_range.label})."
                    )
        else:
            # "No period was given" and "a period was given and not understood"
            # are different sentences, and the resolver already knows which one
            # this is. Saying the first when the second is true is the mistake
            # that makes a wrong answer look like a right one.
            unread = self.dates.unread_period_terms(message)
            page_range = self._page_period(page)
            if context.date_range is not None:
                date_range = context.date_range
                assumptions.append(
                    f"Using the same period as before: {date_range.label}.")
            elif page_range is not None:
                date_range = page_range
                assumptions.append(
                    f"Using the period selected on screen: {date_range.label}.")
            else:
                date_range = self.dates.of_type(DateRangeType.THIS_MONTH)
                assumptions.append(
                    "No period was given, so this covers the current month."
                    if not unread else
                    "This covers the current month."
                )
            if unread:
                assumptions.insert(
                    len(assumptions) - 1,
                    "I could not read "
                    + ", ".join(f"'{term}'" for term in unread)
                    + " as a period."
                )

        if carried:
            assumptions.append(
                "Keeping your earlier filter: "
                + ", ".join(e.label for e in carried) + "."
            )
        # A filter that ended is disclosed as loudly as one that persisted. The
        # two mislead in opposite directions, and a reader who asked for a
        # region cannot otherwise tell whether the territory they named three
        # turns ago is still narrowing the figure in front of them.
        if ended:
            assumptions.append(
                "Your earlier filter no longer applies: "
                + ", ".join(e.label for e in ended) + "."
            )
        # A partial name match narrows the whole answer on the strength of a
        # word fragment, which is the weakest evidence this resolver acts on.
        # Say so. The figure is right for the filter that was applied, and
        # wrong for the question if the fragment was never meant as a name —
        # and only the reader can tell the two apart. It reads what this message
        # named: a carried filter was disclosed on the turn that first read it.
        guessed = [e for e in named if e.match == "partial_name"]
        if guessed:
            assumptions.append(
                "Read "
                + ", ".join(f"'{e.term}' as {e.label}" for e in guessed)
                + ". Say the full name if you meant something else."
            )

        group_by = prediction.group_by or (
            context.group_by if intent == context.intent else []
        )
        # A limit hides rows, so it is inherited only by a question of the same
        # kind — the rule the grouping above already follows. "Top 5 brands"
        # qualified that question; the "total sales কত" after it is not a top
        # five of anything, and a five-row cap on it truncates an answer nobody
        # asked to have truncated.
        inherited_limit = (
            context.limit
            if prediction.limit is None and intent == context.intent else None
        )
        limit = prediction.limit or inherited_limit or DEFAULT_LIMIT
        if inherited_limit:
            assumptions.append(f"Still showing only the top {inherited_limit}.")

        query = StructuredQuery(
            intent=intent,
            date_range=date_range,
            entities=entities,
            group_by=group_by,
            limit=limit,
            compare=prediction.has("growth"),
            language=prediction.language,
            assumptions=assumptions,
        )

        # Permission check runs here — before any tool, before any query.
        self.permissions.check_entities(query.entities)
        return query

    @staticmethod
    def _regroup_intent(previous: Intent, group: GroupBy) -> Intent | None:
        """Keep the metric, change the grouping: "Region-wise দেখাও"."""
        metric = previous.value.split("_")[0]
        mapping = {"SALES": Intent.SALES_DETAIL}
        return mapping.get(metric)

    # -- tool arguments -----------------------------------------------------

    def build_filters(self, query: StructuredQuery) -> ScopeFilters:
        """Entity filters merged with the user's scope."""
        return self.permissions.build_filters(query.entities)

    def tool_arguments(self, tool_name: str, query: StructuredQuery,
                       prediction: IntentPrediction | None = None) -> dict[str, Any]:
        """Arguments for one tool, built only from validated query parts."""
        assert query.date_range is not None
        filters = self.build_filters(query)
        arguments: dict[str, Any] = {
            "date_from": query.date_range.date_from.isoformat(),
            "date_to": query.date_range.date_to.isoformat(),
            "filters": filters.model_dump(mode="json"),
        }

        if tool_name in _GROUPED_TOOLS:
            arguments["group_by"] = self._group_for(tool_name, query).value
            arguments["limit"] = query.limit
            # "contribution" / "share" asks for the same rows with each one's
            # proportion of the whole beside it, so it is a way of reporting an
            # answer rather than a different answer. The tool reads the total in
            # its own query; nothing here computes a share.
            if prediction and prediction.has("share"):
                arguments["include_share"] = True
        if tool_name in _ACHIEVEMENT_TOOLS:
            arguments["group_by"] = self._group_for(tool_name, query).value
            arguments["limit"] = query.limit
            if prediction and prediction.percent_threshold is not None:
                arguments["below_percent"] = prediction.percent_threshold
        if tool_name in _STOCK_TOOLS:
            arguments["limit"] = query.limit
            # "expiring in the next 30 days" — the number the question named
            # becomes the expiry horizon, not a coverage threshold.
            if prediction and prediction.days_threshold is not None:
                arguments["expiring_within_days"] = int(prediction.days_threshold)
            # "which material has the most blocked stock" ranks by blocked
            # stock, not by the total. All four categories come back either
            # way; this decides the order and the headline sentence.
            if prediction and prediction.stock_measure:
                arguments["sort_by"] = prediction.stock_measure
        if tool_name in _TREND_TOOLS:
            span = (query.date_range.date_to - query.date_range.date_from).days
            arguments["granularity"] = "month" if span > 92 else "day"
            arguments["limit"] = 200
        if tool_name in _GROWTH_TOOLS or tool_name == "get_root_cause_analysis":
            compare_from = query.date_range.compare_from or query.date_range.date_from
            compare_to = query.date_range.compare_to or query.date_range.date_to
            arguments["compare_from"] = compare_from.isoformat()
            arguments["compare_to"] = compare_to.isoformat()
        if tool_name in _VOLUME_TOOLS:
            arguments["limit"] = query.limit
            if query.group_by:
                arguments["group_by"] = query.group_by[0].value
        return arguments

    def _group_for(self, tool_name: str, query: StructuredQuery) -> GroupBy:
        """The grouping a tool should use, defaulting sensibly per tool."""
        if tool_name.endswith("_performance"):
            fixed = {
                "get_region_performance": GroupBy.REGION,
                "get_zone_performance": GroupBy.ZONE,
                "get_area_performance": GroupBy.AREA,
                "get_unit_performance": GroupBy.UNIT,
                "get_territory_performance": GroupBy.TERRITORY,
                "get_sub_territory_performance": GroupBy.SUB_TERRITORY,
                "get_material_performance": GroupBy.MATERIAL,
                "get_material_brand_performance": GroupBy.MATERIAL_BRAND,
                "get_material_brand_target_performance": GroupBy.MATERIAL_BRAND,
                "get_material_group_performance": GroupBy.MATERIAL_GROUP,
                "get_customer_performance": GroupBy.CUSTOMER,
                "get_salesforce_performance": GroupBy.SALES_FORCE,
            }
            return fixed[tool_name]
        if query.group_by:
            return query.group_by[0]
        return GroupBy.REGION

    # -- execution ----------------------------------------------------------

    def _execute(self, query: StructuredQuery, message: str,
                 history: Sequence[dict[str, str]]) -> AgentAnswer:
        prediction = detect_intent(message, self.lexicon)
        tool_name = self._select_tool(query, message, history)
        arguments = self.tool_arguments(tool_name, query, prediction)

        ctx = ToolContext(self.session, self.permissions, self.user, self.today)
        invocations = [execute_tool(ctx, tool_name, arguments)]

        for companion in COMPANION_TOOLS.get(query.intent, ()):
            if invocations[0].result is None or not invocations[0].result.values:
                break
            try:
                companion_arguments = self.tool_arguments(companion, query, prediction)
                invocations.append(execute_tool(ctx, companion, companion_arguments))
            except AgentError:
                # A companion figure is a nicety; never fail the answer for it.
                logger.debug("companion tool %s skipped", companion)

        results = [i.result for i in invocations if i.result is not None]
        for result in results:
            outcome = validate_tool_result(result, query.date_range)
            if not outcome.ok:
                logger.error("validation failed for %s: %s", result.tool, outcome.errors)
                raise ValidationFailedError("; ".join(outcome.errors))
            for warning in outcome.warnings:
                logger.debug("validation warning for %s: %s", result.tool, warning)
            if result.chart is not None and not result.chart.data:
                result.chart = None

        answer_text = self.formatter.format(
            query.intent, results, query.date_range,
            results[0].filters if results else {}, query.assumptions,
            # The names behind the codes the answer was filtered by. Only the
            # question's own entities can be named; a code the reader's data
            # scope injected has no entity behind it and keeps its code, which
            # is honest — it narrowed the figure either way.
            entity_labels={e.code: e.label for e in query.entities},
        )
        answer_text = self._maybe_polish(answer_text, message, results, history)

        return AgentAnswer(
            answer=answer_text,
            intent=query.intent,
            query=query,
            results=results,
            invocations=invocations,
            context=ConversationContext(
                intent=query.intent,
                date_range=query.date_range,
                entities=query.entities,
                group_by=query.group_by,
                limit=query.limit if query.limit != DEFAULT_LIMIT else None,
            ),
            language=query.language,
        )

    def _matching_example(self, message: str):
        """The approved example for this exact question, if there is one."""
        if not self.lexicon.examples:
            return None
        key = normalize_phrase(message)
        return self.lexicon.examples.get(key) if key else None

    def _apply_example(self, message: str,
                       prediction: IntentPrediction) -> IntentPrediction:
        """Let an approved example name the intent — but only where none was found.

        The same rule the aliases follow: what the classifier worked out for
        itself is never overridden. An example speaks when the shipped rules
        came back UNKNOWN, which is exactly the case a reviewer approved it to
        cover, and stays silent otherwise.
        """
        if prediction.intent is not Intent.UNKNOWN:
            return prediction
        example = self._matching_example(message)
        if example is None or not example.intent:
            return prediction
        try:
            intent = Intent(example.intent)
        except ValueError:
            # An intent that no longer exists. Ignored rather than raised: the
            # question is simply as unclassified as it was before.
            logger.info("approved example %s names a retired intent %r",
                        example.example_id, example.intent)
            return prediction
        logger.info("intent %s taken from approved example %s", intent.value,
                    example.example_id)
        return replace(prediction, intent=intent)

    def _select_tool(self, query: StructuredQuery, message: str,
                     history: Sequence[dict[str, str]]) -> str:
        """Ask the model which tool to run; fall back to the intent mapping.

        The model may only pick from tools that serve the detected intent, so a
        model error cannot redirect a sales question at an unrelated report.
        """
        default = TOOL_BY_INTENT.get(query.intent)
        if default is None:
            raise UnsupportedQuestionError()

        allowed = set(tools_for_intent(query.intent)) | {default}

        # An approved example is a person's answer to "which report does this
        # question want", so it outranks the intent's default tool — but only
        # within that intent's own allow-list, and only for a tool that still
        # exists. Its stored *arguments* are never reused: those carried one
        # past caller's dates and filters, and this question builds its own.
        example = self._matching_example(message)
        if example is not None and example.tool_name in allowed:
            if example.tool_name in REGISTRY:
                lexicon.note_use(self.session, example.example_id)
                return example.tool_name

        if not self.llm.available:
            return default
        definitions = [REGISTRY[name].openai_schema() for name in allowed
                       if name in REGISTRY]
        # The same approved bank the deterministic shortcut reads, shown to the
        # model as worked examples. It only reaches here when the question was
        # *not* an exact match — an exact match returned above without asking.
        system = (
            build_system_prompt(self.user.role, self.user.describe_scope(),
                                query.language)
            + "\n" + PLANNER_PROMPT
            + planner_examples(self.lexicon.examples.values())
        )
        try:
            response = self.llm.plan(
                conversation_messages(system, history, message), definitions
            )
        except Exception:  # noqa: BLE001 - the deterministic path still works
            logger.warning("LLM planning failed; using the deterministic planner")
            return default

        if response.wants_tool:
            chosen = response.tool_calls[0].name
            if chosen in allowed:
                return chosen
            logger.info("model proposed tool %r outside the intent's allow-list", chosen)
        return default

    def _maybe_polish(self, text: str, message: str, results: Sequence[ToolResult],
                      history: Sequence[dict[str, str]]) -> str:
        """Optionally let the model rephrase — never recompute.

        The model is handed the already-formatted answer. If it returns anything
        unusable the deterministic text is kept, so the worst case is plainer
        prose, never a wrong number.
        """
        if not self.llm.available or not results:
            return text
        system = build_system_prompt(self.user.role, self.user.describe_scope())
        payload = (
            f"{ANSWER_PROMPT}\n\nUser question:\n{message}\n\n"
            f"Validated report (already formatted — reuse these figures verbatim):\n{text}"
        )
        try:
            response = self.llm.write_answer([
                {"role": "system", "content": system},
                {"role": "user", "content": payload},
            ])
        except Exception:  # noqa: BLE001
            return text
        rephrased = (response.text or "").strip()
        if not rephrased:
            return text
        # The one check that makes the docstring above true. A model asked to
        # reuse figures verbatim usually does; when it does not — a rounded
        # crore, a total it worked out itself — the sentence reads as fluently
        # as the right one and there is nothing on screen to tell them apart.
        # Any number the deterministic answer does not contain sends the whole
        # rephrasing back, because a part-invented answer is not repairable.
        if not response_grounded_in(rephrased, numbers_in(text)):
            logger.warning("Discarded a rephrasing that quoted an unsourced figure.")
            return text
        return rephrased


__all__ = [
    "Orchestrator",
    "AgentAnswer",
    "ConversationContext",
    "TOOL_BY_INTENT",
    "COMPANION_TOOLS",
]
