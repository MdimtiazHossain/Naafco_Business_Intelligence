"""Refusals this package raises.

Subclasses of :class:`app.ai.exceptions.AgentError` rather than a parallel
hierarchy, so the message-safety contract is the same one the agent already
keeps: ``user_message`` is shown verbatim, and therefore never contains SQL, a
connection string, a stack trace or an internal identifier.

Every one of these is a *state* refusal — the request was well-formed and the
data says no — which is why the API answers them with 409 rather than 400 or
500. The reason is the whole value to a planner, so it is shown rather than
masked.
"""

from __future__ import annotations

from ..ai.exceptions import AgentError


class TargetManagementError(AgentError):
    """Base class for every refusal in this package."""

    code = "TARGET_MANAGEMENT_ERROR"
    user_message = "That target action could not be completed."


class PlanScopeConflict(TargetManagementError):
    """A plan already exists for this financial year, period and org slice.

    Not an error to route around: the whole point of one plan per scope is that
    "the FY 2026-27 Q1 target for SL001" names one thing. A second target for
    the same scope is a new *version* of that plan.
    """

    code = "TARGET_PLAN_SCOPE_CONFLICT"

    def __init__(self, plan_code: str) -> None:
        super().__init__(
            f"plan scope already held by {plan_code}",
            user_message=(
                f"A target plan for this financial year, period and sales line "
                f"already exists ({plan_code}). Create a new version of it "
                f"rather than a second plan."
            ),
            details={"plan_code": plan_code},
        )


class UnknownScopeCode(TargetManagementError):
    """A company, business unit or sales line the master data does not have.

    Checked here rather than left to a foreign key: the message can name which
    code and which master, which an integrity error cannot.
    """

    code = "TARGET_SCOPE_CODE_UNKNOWN"

    def __init__(self, level_label: str, code: str) -> None:
        super().__init__(
            f"{level_label} {code} not in master data",
            user_message=(
                f"{level_label} {code} is not in the master data. Load or correct "
                f"the master before planning against it."
            ),
            details={"level": level_label, "code": code},
        )


class ScopeMismatch(TargetManagementError):
    """The chosen business unit or sales line sits under a different parent."""

    code = "TARGET_SCOPE_MISMATCH"

    def __init__(self, child_label: str, child_code: str,
                 parent_label: str, parent_code: str) -> None:
        super().__init__(
            f"{child_code} does not belong to {parent_code}",
            user_message=(
                f"{child_label} {child_code} does not belong to {parent_label} "
                f"{parent_code}. Choose one that does."
            ),
            details={"child": child_code, "parent": parent_code},
        )


class PlanNotFound(TargetManagementError):
    """No plan with that identifier, or none the caller may see."""

    code = "TARGET_PLAN_NOT_FOUND"
    user_message = "That target plan was not found."


class VersionNotFound(TargetManagementError):
    """No version with that identifier."""

    code = "TARGET_VERSION_NOT_FOUND"
    user_message = "That target version was not found."


class VersionFrozen(TargetManagementError):
    """The version has been approved or locked and is no longer editable.

    The module's central promise, enforced: an approved target is never
    overwritten. Changing it means a new version through approval.
    """

    code = "TARGET_VERSION_FROZEN"

    def __init__(self, version_label: str, status: str) -> None:
        super().__init__(
            f"{version_label} is {status}",
            user_message=(
                f"{version_label} is {status.replace('_', ' ').lower()} and cannot "
                f"be edited. Create the next version to change it."
            ),
            details={"status": status},
        )


class InvalidTransition(TargetManagementError):
    """A status change the workflow does not allow."""

    code = "TARGET_INVALID_TRANSITION"

    def __init__(self, current: str, requested: str) -> None:
        pretty = lambda value: value.replace("_", " ").lower()  # noqa: E731
        super().__init__(
            f"{current} -> {requested} is not a permitted transition",
            user_message=(
                f"A target that is {pretty(current)} cannot move straight to "
                f"{pretty(requested)}."
            ),
            details={"from": current, "to": requested},
        )


class ReasonRequired(TargetManagementError):
    """A version after the first, or a revision, with no stated reason."""

    code = "TARGET_REASON_REQUIRED"
    user_message = (
        "A reason is required. It is kept with the version so anyone reading "
        "the target later can see why it changed."
    )


__all__ = [
    "TargetManagementError",
    "PlanScopeConflict",
    "UnknownScopeCode",
    "ScopeMismatch",
    "PlanNotFound",
    "VersionNotFound",
    "VersionFrozen",
    "InvalidTransition",
    "ReasonRequired",
]
