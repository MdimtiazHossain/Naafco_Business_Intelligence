"""Agent exceptions.

Each carries a **user-safe** message. Nothing here ever contains SQL, a
connection string, a stack trace or the system prompt: these messages are shown
verbatim to the user, so they must be safe by construction.
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Base class. ``user_message`` is what the user is allowed to see."""

    user_message = "I couldn't complete that request. Please try again."
    code = "AGENT_ERROR"

    def __init__(self, message: str | None = None, *, user_message: str | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message or self.user_message)
        if user_message:
            self.user_message = user_message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error_code": self.code, "message": self.user_message, **self.details}


class PermissionDeniedError(AgentError):
    """The user asked for data outside their authorised scope."""

    code = "PERMISSION_DENIED"
    user_message = "You don't have permission to access this information."


class ScopeNotEnforceable(PermissionDeniedError):
    """This report cannot express part of the caller's data scope.

    Distinct from a plain permission denial, and the difference is worth a code
    of its own: the caller has not asked for something outside their scope, they
    have asked for a report that cannot be *narrowed* to it. Nothing they type
    will fix that and retrying will not help — an administrator changing their
    access is the only answer, so the message has to say so rather than reading
    as "you may not see this".

    ``PermissionDeniedError`` all the same, because the outcome is identical:
    the figures exist and this caller does not get them.
    """

    code = "SCOPE_NOT_ENFORCEABLE"


class AmbiguousEntityError(AgentError):
    """A name matched more than one master record; the agent must not guess."""

    code = "AMBIGUOUS_ENTITY"
    user_message = "Please specify which entity you mean."

    def __init__(self, term: str, candidates: list[dict[str, Any]]) -> None:
        options = " or ".join(
            f"the {c['label']} {c['entity_type'].replace('_', ' ')}" for c in candidates[:4]
        )
        super().__init__(
            f"'{term}' matched {len(candidates)} master records",
            user_message=f"Do you mean {options}?",
            details={"term": term, "candidates": candidates[:8]},
        )


class EntityNotFoundError(AgentError):
    """A name matched no master record. Never resolved by inventing one."""

    code = "ENTITY_NOT_FOUND"

    def __init__(self, term: str, entity_type: str | None = None) -> None:
        where = f" in the {entity_type.replace('_', ' ')} master data" if entity_type else ""
        super().__init__(
            f"'{term}' not found{where}",
            user_message=(
                f"I couldn't find '{term}'{where}. Please check the name or code — "
                "I only report on official master data."
            ),
            details={"term": term, "entity_type": entity_type},
        )


class DateResolutionError(AgentError):
    """A period expression could not be resolved unambiguously."""

    code = "DATE_NOT_RESOLVED"
    user_message = (
        "I couldn't work out which period you mean. Try 'this month', 'last 30 days' "
        "or an explicit range like '2026-08-01 to 2026-08-31'."
    )


class NoDataError(AgentError):
    """The query was valid but the warehouse holds nothing for it."""

    code = "NO_DATA"
    user_message = "No data found for the selected period and filters."


class ToolExecutionError(AgentError):
    """A tool failed. The real cause is logged, never returned."""

    code = "TOOL_FAILED"
    user_message = "I couldn't generate the report right now. Please try again."


class ValidationFailedError(AgentError):
    """Tool output failed validation, so it must not reach the user."""

    code = "VALIDATION_FAILED"
    user_message = (
        "I found a problem while checking those numbers, so I'm not showing them. "
        "Please try again."
    )


class UnsupportedQuestionError(AgentError):
    """The question is outside the agent's business-reporting scope."""

    code = "UNSUPPORTED"
    user_message = (
        "I can only answer questions about sales, material stock and targets "
        "from the company's data warehouse."
    )


__all__ = [
    "AgentError",
    "PermissionDeniedError",
    "ScopeNotEnforceable",
    "AmbiguousEntityError",
    "EntityNotFoundError",
    "DateResolutionError",
    "NoDataError",
    "ToolExecutionError",
    "ValidationFailedError",
    "UnsupportedQuestionError",
]
