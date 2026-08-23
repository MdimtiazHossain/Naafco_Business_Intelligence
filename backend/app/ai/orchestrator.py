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
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy.orm import Session

from ..etl.mapping import MasterDataIndex
from .date_resolver import DateResolver
from .entity_resolver import EntityResolver
from .exceptions import (
    AgentError,
    AmbiguousEntityError,
    NoDataError,
    PermissionDeniedError,
    UnsupportedQuestionError,
    ValidationFailedError,
)
from .intent import IntentPrediction, detect_intent
from .llm import LLMClient, NullLLMClient
from .permission_filter import PermissionFilter, UserContext
from .prompts import (
    ANSWER_PROMPT,
    INJECTION_REFUSAL,
    PLANNER_PROMPT,
    build_system_prompt,
    conversation_messages,
    sanitize_message,
)
from .response_formatter import ResponseFormatter
from .schemas import (
    ChartSpec,
    DateRangeType,
    EntityType,
    GroupBy,
    Intent,
    ResolvedDateRange,
    ResolvedEntity,
    ScopeFilters,
    StructuredQuery,
    ToolResult,
)
from .tools import REGISTRY, ToolContext, ToolInvocation, execute_tool, tools_for_intent
from .validators import validate_tool_result

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
}

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
        self.master_index = MasterDataIndex(session)
        self.permissions = PermissionFilter(session, user, self.master_index)
        self.entities = EntityResolver(session)
        self.dates = DateResolver(today=self.today)
        self.formatter = ResponseFormatter()

    # -- pipeline -----------------------------------------------------------

    def answer(self, message: str, context: ConversationContext | None = None,
               history: Sequence[dict[str, str]] = ()) -> AgentAnswer:
        started = time.perf_counter()
        context = context or ConversationContext()

        cleaned, injections = sanitize_message(message)
        prediction = detect_intent(cleaned or message)

        if injections and not cleaned:
            # Nothing left once the injection is removed: there was no question.
            return self._finish(AgentAnswer(
                answer=INJECTION_REFUSAL, intent=Intent.UNKNOWN,
                language=prediction.language, injection_detected=True,
                error_code="PROMPT_INJECTION",
            ), started)

        try:
            query = self.build_query(cleaned or message, prediction, context)
            answer = self._execute(query, cleaned or message, history)
        except AgentError as exc:
            return self._finish(AgentAnswer(
                answer=exc.user_message,
                intent=prediction.intent,
                language=prediction.language,
                needs_clarification=isinstance(exc, AmbiguousEntityError),
                error_code=exc.code,
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

    def build_query(self, message: str, prediction: IntentPrediction,
                    context: ConversationContext) -> StructuredQuery:
        """Assemble and validate the structured query for a message."""
        assumptions: list[str] = []

        entities = self.entities.resolve_message(
            message, exclude_types=self._excluded_entity_types(prediction, context))
        inherited_entities = False
        if not entities and context.entities:
            entities = list(context.entities)
            inherited_entities = True

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
        elif context.date_range is not None:
            date_range = context.date_range
            assumptions.append(f"Using the same period as before: {date_range.label}.")
        else:
            date_range = self.dates.of_type(DateRangeType.THIS_MONTH)
            assumptions.append(
                "No period was given, so this covers the current month."
            )

        if inherited_entities and entities:
            assumptions.append(
                "Keeping your earlier filter: "
                + ", ".join(e.label for e in entities) + "."
            )
        elif entities:
            # A partial name match narrows the whole answer on the strength of a
            # word fragment, which is the weakest evidence this resolver acts on.
            # Say so. The figure is right for the filter that was applied, and
            # wrong for the question if the fragment was never meant as a name —
            # and only the reader can tell the two apart.
            guessed = [e for e in entities if e.match == "partial_name"]
            if guessed:
                assumptions.append(
                    "Read "
                    + ", ".join(f"'{e.term}' as {e.label}" for e in guessed)
                    + ". Say the full name if you meant something else."
                )

        group_by = prediction.group_by or (
            context.group_by if intent == context.intent else []
        )
        limit = prediction.limit or context.limit or 20

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
        prediction = detect_intent(message)
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
                limit=query.limit,
            ),
            language=query.language,
        )

    def _select_tool(self, query: StructuredQuery, message: str,
                     history: Sequence[dict[str, str]]) -> str:
        """Ask the model which tool to run; fall back to the intent mapping.

        The model may only pick from tools that serve the detected intent, so a
        model error cannot redirect a sales question at an unrelated report.
        """
        default = TOOL_BY_INTENT.get(query.intent)
        if default is None:
            raise UnsupportedQuestionError()

        if not self.llm.available:
            return default

        allowed = set(tools_for_intent(query.intent)) | {default}
        definitions = [REGISTRY[name].openai_schema() for name in allowed
                       if name in REGISTRY]
        system = build_system_prompt(self.user.role, self.user.describe_scope(),
                                     query.language) + "\n" + PLANNER_PROMPT
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
        if response.text and response.text.strip():
            return response.text.strip()
        return text


__all__ = [
    "Orchestrator",
    "AgentAnswer",
    "ConversationContext",
    "TOOL_BY_INTENT",
    "COMPANION_TOOLS",
]
