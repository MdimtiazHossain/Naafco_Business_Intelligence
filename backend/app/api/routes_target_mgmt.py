"""Target Management API: plans, versions and the scope they are built on.

Guarded by ``SectionKey.TARGET_MANAGEMENT``, which is off by default for every
role. Reading achievement against a target is reporting and lives in the Target
section beside it; *setting* the target the whole sales force is measured on is
not, and a role that should see one does not automatically get the other.

Actions are enforced per endpoint rather than per router. Creating a plan is
``CREATE``, moving a version through the workflow is ``EDIT``, and approving is
its own ``APPROVE`` — so a planner who may build a target and an approver who
may sign one off can be two different people, which is the entire point of the
approval matrix downstream.

Every state change is audited **twice, on purpose**: once into ``target_audit``
by the service layer, which is the business trail a planner reads on the Audit
Trail screen, and once into the security log here, which is where an
administrator reads it beside a login or a permission change. Neither answers
the other's question.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..auth import audit
from ..auth.permissions import allowed_actions, require_action, require_section
from ..database.models_ai import AuditAction
from ..database.models_target import RevisionStatus, TargetLevel, TargetStatus
from ..security.sections import Action, SectionKey
from ..upload import files as upload_files
from ..targetmgmt import (
    adjustments,
    approvals,
    bulk,
    compare,
    country,
    dashboard,
    factors,
    history,
    matrix,
    plans,
    readiness,
    reconcile,
    review,
    revisions,
)
from ..targetmgmt import audit as target_audit
from ..targetmgmt import lock as target_lock
from ..targetmgmt import engine as allocation_engine
from ..targetmgmt import jobs as allocation_jobs
from ..targetmgmt.errors import TargetManagementError
from .deps import get_session, internal_error

logger = logging.getLogger("app.api.target_management")

router = APIRouter(prefix="/api/target-management", tags=["target-management"])

_VIEW = require_section(SectionKey.TARGET_MANAGEMENT)
_CREATE = require_action(SectionKey.TARGET_MANAGEMENT, Action.CREATE)
_EDIT = require_action(SectionKey.TARGET_MANAGEMENT, Action.EDIT)
#: Two separate actions, and never collapsed into one. Signing off on the figure
#: a sales force is measured on and asking for it to be changed are held by
#: different people: a Sales Officer may revise their own sub-territory target
#: and must never approve one.
_APPROVE = require_action(SectionKey.TARGET_MANAGEMENT, Action.APPROVE)
_REVISE = require_action(SectionKey.TARGET_MANAGEMENT, Action.REVISE)
#: Loading a country target from a file is ``UPLOAD`` rather than
#: ``EDIT``: it is the same act as typing one, and the section keeps the
#: two separable so a planner who may adjust a figure by hand and one who
#: may load three hundred at once can be different people.
_UPLOAD = require_action(SectionKey.TARGET_MANAGEMENT, Action.UPLOAD)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PlanRequest(BaseModel):
    """The scope a plan fixes. ``extra="forbid"`` as every schema here is."""

    model_config = ConfigDict(extra="forbid")

    financial_year: str = Field(min_length=1, max_length=32)
    target_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"]
    company_code: str = Field(min_length=1, max_length=64)
    bu_code: str = Field(min_length=1, max_length=64)
    sales_line_code: str = Field(min_length=1, max_length=64)
    basis_financial_years: str | None = Field(default=None, max_length=128)


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Required, and not merely by this schema — ``plans.create_version``
    #: refuses an empty one too, so a caller that bypasses the API cannot
    #: create an unexplained version either.
    reason: str = Field(min_length=1, max_length=1000)


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "DRAFT", "ALLOCATION_IN_PROGRESS", "ALLOCATED", "UNDER_REVIEW",
        "PARTIALLY_APPROVED", "APPROVED", "REJECTED", "LOCKED", "REVISED",
    ]
    reason: str | None = Field(default=None, max_length=1000)


class CountryLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_code: str = Field(min_length=1, max_length=64)
    #: Typed as ``str`` deliberately, not ``float``. Pydantic would happily read
    #: ``"12.5"`` and reject ``"12,5OO"`` with a schema error that says nothing
    #: about which material or why; ``country._coerce_volume`` refuses it by
    #: name, and refuses it identically for a caller that never touched this
    #: schema. A volume is never coerced from something that is not a number.
    target_volume: str | float | int


class CountryTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: One request may carry the whole grid. Capped so a single call cannot be
    #: turned into an unbounded write; the Material Master is in the hundreds,
    #: so this is far above any real country target.
    lines: list[CountryLineInput] = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _commit(session: Session, user: UserContext, http_request: Request, *,
            resource: str, audit_action: str, detail: dict[str, Any],
            work) -> Any:
    """Run one write, audit it and commit — or surface a clean refusal.

    409 rather than 400 for a :class:`TargetManagementError`: the request was
    well-formed and the *state* refuses it — a scope already planned, a version
    already approved, a transition the workflow does not allow. The reason is
    the whole value to a planner, so it is shown rather than masked. A write
    against something that is not there is the one exception and answers 404,
    because "there is no such plan" is a different problem from "that plan says
    no".
    """
    try:
        result = work()
        audit.record(
            session, action=audit_action, user_id=user.user_id,
            username=user.username, resource=resource,
            ip_address=audit.client_ip(http_request), detail=detail,
        )
        session.commit()
        return result
    except TargetManagementError as exc:
        session.rollback()
        raise HTTPException(
            status_code=_status_for(exc.code),
            detail={"error_code": exc.code, "message": exc.user_message},
        ) from exc
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - internals never reach the caller
        session.rollback()
        raise internal_error(exc, "target-management") from exc


def _status_for(error_code: str) -> int:
    """404 for something that is not there, 409 for something that says no.

    One rule, shared by the read and write paths, so a missing plan does not
    answer 404 to a GET and 409 to a POST.
    """
    return (status.HTTP_404_NOT_FOUND if error_code.endswith("_NOT_FOUND")
            else status.HTTP_409_CONFLICT)


def _read(work) -> Any:
    try:
        return work()
    except TargetManagementError as exc:
        raise HTTPException(
            status_code=_status_for(exc.code),
            detail={"error_code": exc.code, "message": exc.user_message},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "target-management") from exc


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@router.get("/options")
def options(session: Session = Depends(get_session),
            user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """Everything the plan form needs, plus what this caller may do.

    The scope lists are read from the master data on every call, so a company or
    sales line added there is offered here with no change in this module and
    none in the browser.

    ``actions`` is included because the frontend draws its buttons from it. That
    is presentation, not security: every endpoint below re-resolves the same
    permission server-side, and a hand-typed request returns 403 whatever the
    browser was told.
    """
    return _read(lambda: {
        **plans.scope_options(session),
        "actions": allowed_actions(session, user, SectionKey.TARGET_MANAGEMENT),
    })


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@router.get("/plans")
def list_plans(
    financial_year: str | None = Query(default=None, max_length=32),
    plan_status: str | None = Query(default=None, max_length=32),
    company_code: str | None = Query(default=None, max_length=64),
    sales_line_code: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """Existing plans, newest financial year first."""
    def work() -> dict[str, Any]:
        rows, total = plans.list_plans(
            session, user, financial_year=financial_year,
            plan_status=plan_status, company_code=company_code,
            sales_line_code=sales_line_code, limit=limit, offset=offset,
        )
        return {"plans": rows, "total": total}

    return _read(work)


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, session: Session = Depends(get_session),
             user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        plan = plans.get_plan(session, plan_id)
        current = plans.current_version(session, plan_id)
        return {
            "plan": plans.plan_to_dict(plan, current),
            "versions": plans.list_versions(session, plan_id),
        }

    return _read(work)


@router.post("/plans", status_code=status.HTTP_201_CREATED)
def create_plan(
    request: PlanRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_CREATE),
) -> dict[str, Any]:
    """Create a plan and its first version.

    Refuses with 409 when the scope is already planned. That is not an obstacle
    to route around: one plan per scope is what makes "the FY 2026-27 Q1 target
    for SL001" name one thing, and the refusal says so and names the plan that
    holds it.
    """
    scope = plans.PlanScope(
        financial_year=request.financial_year.strip(),
        target_period=request.target_period,
        company_code=request.company_code.strip(),
        bu_code=request.bu_code.strip(),
        sales_line_code=request.sales_line_code.strip(),
    )

    def work() -> dict[str, Any]:
        plan = plans.create_plan(
            session, user, scope=scope,
            basis_financial_years=request.basis_financial_years,
        )
        current = plans.current_version(session, plan.plan_id)
        return {"plan": plans.plan_to_dict(plan, current)}

    return _commit(
        session, user, http_request, resource="target_plan",
        audit_action=AuditAction.TARGET_PLAN_CREATED,
        detail={
            "financial_year": scope.financial_year,
            "target_period": scope.target_period,
            "company_code": scope.company_code,
            "bu_code": scope.bu_code,
            "sales_line_code": scope.sales_line_code,
        },
        work=work,
    )


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@router.get("/plans/{plan_id}/versions")
def list_versions(plan_id: int, session: Session = Depends(get_session),
                  user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    return _read(lambda: {"versions": plans.list_versions(session, plan_id)})


@router.post("/plans/{plan_id}/versions", status_code=status.HTTP_201_CREATED)
def create_version(
    plan_id: int,
    request: VersionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_CREATE),
) -> dict[str, Any]:
    """Create the next version from the current one and make it current.

    The country volumes are copied so the new version starts from what was
    there, and the version it came from keeps everything it had — an approved
    V2 whose numbers moved to V3 would no longer be the thing that was approved.
    """
    def work() -> dict[str, Any]:
        version = plans.create_version(
            session, user, plan_id=plan_id, reason=request.reason)
        return {"version": plans.version_to_dict(version)}

    return _commit(
        session, user, http_request, resource="target_version",
        audit_action=AuditAction.TARGET_VERSION_CREATED,
        detail={"plan_id": plan_id, "reason": request.reason[:200]},
        work=work,
    )


@router.patch("/versions/{version_id}/status")
def set_version_status(
    version_id: int,
    request: StatusRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    """Move a version through the workflow.

    ``EDIT`` for now, and deliberately not ``APPROVE``: this endpoint moves the
    *version*, and approving a target is an act on a node inside it, which
    arrives with the approval workflow. Wiring approval to this endpoint would
    let a whole plan be approved without any node having been looked at.
    """
    def work() -> dict[str, Any]:
        version = plans.set_version_status(
            session, user, version_id=version_id,
            new_status=request.status, reason=request.reason,
        )
        return {"version": plans.version_to_dict(version)}

    return _commit(
        session, user, http_request, resource="target_version",
        audit_action=AuditAction.TARGET_VERSION_UPDATED,
        detail={"version_id": version_id, "status": request.status},
        work=work,
    )


# ---------------------------------------------------------------------------
# Country target
# ---------------------------------------------------------------------------


def _version_and_plan(session: Session, version_id: int):
    """A version with the plan it belongs to. Both are needed on every call here.

    The plan is what fixes the company a material must belong to, so no country
    target operation can be checked without it.
    """
    version = plans.get_version(session, version_id)
    plan = plans.get_plan(session, version.plan_id)
    return version, plan


@router.get("/versions/{version_id}/country-target")
def country_target(version_id: int, session: Session = Depends(get_session),
                   user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """The typed country volumes, with quantity and value derived on this read.

    ``totals`` carries ``null`` for quantity and value whenever any line lacks a
    Conversion Factor or a Transfer Price, and ``notes`` says which materials
    and what to do about it. That is deliberate: a total over a mixture of
    derivable and non-derivable lines is short by an unknown amount, and a
    plausible wrong figure is worse than an honest ``n/a``.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        lines = country.list_lines(session, version_id)
        summary = country.totals(lines)
        return {
            "plan": plans.plan_to_dict(plan),
            "version": plans.version_to_dict(version, line_count=len(lines)),
            "lines": [line.to_dict() for line in lines],
            "totals": summary,
            "notes": country.notes(summary),
            # Whether the version itself accepts a write. The browser draws its
            # inputs read-only from this; the endpoint below re-checks it, so a
            # hand-typed request against an approved version is refused anyway.
            "editable": version.status in TargetStatus.EDITABLE,
        }

    return _read(work)


@router.get("/versions/{version_id}/available-materials")
def available_materials(version_id: int, session: Session = Depends(get_session),
                        user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """Materials that may still be added: the plan's company, minus what is on it.

    Read from the Material Master on every call, so a material loaded there
    appears here with no change in this module and none in the browser.
    """
    def work() -> dict[str, Any]:
        _, plan = _version_and_plan(session, version_id)
        return {
            "materials": country.available_materials(session, plan, version_id),
        }

    return _read(work)


@router.put("/versions/{version_id}/country-target")
def set_country_target(
    version_id: int,
    request: CountryTargetRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    """Set country volumes, all of them or none.

    Every line is validated before any is written, so a grid with one bad cell
    does not leave half its changes applied — the shape the ETL gives an import,
    for the same reason. A line whose value did not change writes nothing and
    audits nothing: re-saving a grid is not a revision of every number on it.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        outcome = country.set_lines(
            session, user, version=version, plan=plan,
            entries=[(line.material_code, line.target_volume)
                     for line in request.lines],
        )
        lines = country.list_lines(session, version_id)
        summary = country.totals(lines)
        return {
            "written": outcome,
            "lines": [line.to_dict() for line in lines],
            "totals": summary,
            "notes": country.notes(summary),
            "editable": version.status in TargetStatus.EDITABLE,
        }

    return _commit(
        session, user, http_request, resource="target_country_line",
        audit_action=AuditAction.TARGET_COUNTRY_TARGET_EDITED,
        detail={"version_id": version_id, "lines": len(request.lines)},
        work=work,
    )


# ---------------------------------------------------------------------------
# Historical analysis
# ---------------------------------------------------------------------------


@router.get("/versions/{version_id}/history")
def historical_analysis(version_id: int,
                        session: Session = Depends(get_session),
                        user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """Two financial years of actual sales, and the basis they imply.

    Read from ``vw_sales_detail`` through the same scope layer as every other
    report, with the plan's company / business unit / sales line applied first
    and the caller's own data scope merged on top — so a regional manager sees
    the history of their region, and this figure and the Sales page cannot
    disagree.

    Nothing here is estimated. A material with no sales in a basis year reports
    ``null`` rather than zero, and its suggested basis says ``NO_HISTORY``
    instead of proposing a distribution built on nothing.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        analysis = history.analyse(session, user, plan=plan,
                                   version_id=version_id)
        return {
            "plan": plans.plan_to_dict(plan),
            "version": plans.version_to_dict(version),
            **analysis,
        }

    return _read(work)


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


class AllocationRequest(BaseModel):
    """Factor configuration for one run. Everything unstated takes its default."""

    model_config = ConfigDict(extra="forbid")

    #: ``{factor_key: weight}``. Relative, so 40/15/15 and 8/3/3 allocate alike.
    weights: dict[str, float] | None = None
    enabled: dict[str, bool] | None = None


@router.get("/allocation-factors")
def allocation_factors(user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """The factor catalogue, derived from the engine's own declaration.

    A factor added, renamed or retired in ``targetmgmt.factors`` appears here
    with no change in this module and none in the browser — the same rule every
    other registry in this project follows. Two of the eight report
    ``supported: false`` with the reason: the platform holds no customer or
    territory potential, and deriving one from sales history would make the
    factor a second copy of Historical Sales Contribution.
    """
    return {
        "factors": factors.describe(),
        "defaults": factors.FactorSettings.defaults().to_dict(),
        "stages": [{"key": key, "label": label}
                   for key, label in allocation_engine.STAGES],
        "new_node_seed_share": factors.NEW_NODE_SEED_SHARE,
        "growth_guidance_percent": history.GROWTH_GUIDANCE_PERCENT,
    }


@router.get("/versions/{version_id}/allocation")
def allocation_state(version_id: int, session: Session = Depends(get_session),
                     user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """The version's current allocation, its reconciliation and its last run.

    Reconciliation is **recomputed from the stored rows** on every read rather
    than served from the job's saved verdict. The saved one records what was
    true when the run finished; this one records what is true now, and the two
    differ the moment somebody edits a country volume without re-allocating —
    which is exactly the state a planner needs to see.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        lines = country.list_lines(session, version_id)
        country_target = {
            line.material_code: Decimal(str(line.target_volume))
            for line in lines if line.target_volume
        }
        verdict = reconcile.check(session, version_id=version_id,
                                  country_target=country_target)
        job = allocation_jobs.latest_for_version(session, version_id)
        return {
            "plan": plans.plan_to_dict(plan),
            "version": plans.version_to_dict(version, line_count=len(lines)),
            "reconciliation": verdict.to_dict(),
            "job": allocation_jobs.to_dict(job) if job is not None else None,
            "has_allocation": verdict.node_count > 0,
            "editable": version.status in TargetStatus.EDITABLE,
        }

    return _read(work)


@router.post("/versions/{version_id}/allocation",
             status_code=status.HTTP_202_ACCEPTED)
def start_allocation(
    version_id: int,
    request: AllocationRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    """Queue an allocation run. Answers 202 with a job to poll.

    Deliberately **not** synchronous. An allocation reads two years of sales and
    writes tens of thousands of rows; done inside the request it would hold a
    worker for the whole run and, on a full-year plan across a real customer
    base, would simply never come back.
    """
    settings = factors.FactorSettings.from_request(request.model_dump())

    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        job = allocation_jobs.submit(session, user, plan=plan, version=version,
                                     settings=settings)
        return {"job": allocation_jobs.to_dict(job)}

    result = _commit(
        session, user, http_request, resource="target_allocation",
        audit_action=AuditAction.TARGET_ALLOCATION_STARTED,
        detail={"version_id": version_id},
        work=work,
    )
    # After the commit, never before: a worker that opened its own session
    # while this transaction was still open would not find the job row it is
    # meant to work on, and would fail on a fast machine while passing on a
    # slow one.
    allocation_jobs.run_after_commit(session)
    return result


@router.get("/allocation-jobs/{job_uuid}")
def allocation_job(job_uuid: str, session: Session = Depends(get_session),
                   user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """One run's progress, for the browser to poll.

    Served from the job row rather than from process memory, so the answer
    survives a page reload, a second uvicorn worker and a restart. That is
    affordable here and not in the upload centre because an allocation's
    expensive half holds no write lock — see ``targetmgmt.jobs``.
    """
    def work() -> dict[str, Any]:
        state = allocation_jobs.state(session, job_uuid)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "TARGET_ALLOCATION_JOB_NOT_FOUND",
                        "message": "That allocation run was not found."},
            )
        return state

    return _read(work)


# ---------------------------------------------------------------------------
# Readiness, history and adjustments
# ---------------------------------------------------------------------------


class AdjustmentRequest(BaseModel):
    """One management adjustment: a node, a volume and a reason."""

    model_config = ConfigDict(extra="forbid")

    level: Literal["zone", "region", "area", "unit", "territory",
                   "sub_territory", "customer"]
    node_code: str = Field(min_length=1, max_length=64)
    #: Optional. Absent adjusts every material at the node; stated adjusts one.
    material_code: str | None = Field(default=None, max_length=64)
    #: Absolute and signed — ``+500``, ``-1200``. Never a percentage: a uniform
    #: percentage re-normalised is a mathematical no-op.
    adjustment_volume: float
    reason: str = Field(min_length=1, max_length=1000)


@router.get("/versions/{version_id}/readiness")
def allocation_readiness(version_id: int,
                         session: Session = Depends(get_session),
                         user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """The pre-flight check: everything an allocation would find, before it runs.

    Read-only and cheap enough to answer on every page load, which is the whole
    point — discovering that no customer carries a sub-territory *halfway
    through* a background job is the worst possible time to discover it.

    Every check answers with one of the five data states rather than a boolean:
    "no sales loaded", "sales loaded but none states a volume", "customers
    present but unmapped" and "no potential master exists" are four different
    situations with four different fixes, and a tick-or-cross cannot tell them
    apart.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return {
            "plan": plans.plan_to_dict(plan),
            "version": plans.version_to_dict(version),
            **readiness.check(session, user, plan=plan, version=version),
        }

    return _read(work)


@router.get("/versions/{version_id}/allocation-runs")
def allocation_runs(
    version_id: int,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """Every allocation run against this version, newest first.

    Failed runs are listed beside successful ones, deliberately: the question a
    planner brings here is usually "why did the last attempt not work", and a
    list showing only successes could not answer it.
    """
    def work() -> dict[str, Any]:
        _version_and_plan(session, version_id)
        rows, total = allocation_jobs.history(session, version_id=version_id,
                                              limit=limit, offset=offset)
        return {"runs": rows, "total": total}

    return _read(work)


@router.get("/versions/{version_id}/adjustments")
def list_adjustments(version_id: int, session: Session = Depends(get_session),
                     user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """Standing management adjustments, which the next run will apply."""
    def work() -> dict[str, Any]:
        _version_and_plan(session, version_id)
        return {
            "adjustments": [adjustments.to_dict(row) for row in
                            adjustments.for_version(session, version_id)],
        }

    return _read(work)


@router.put("/versions/{version_id}/adjustments")
def set_adjustment(
    version_id: int,
    request: AdjustmentRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    """Record an adjustment for the next allocation run to apply.

    Stored rather than applied in place: an adjustment is an *input* to the
    engine, so re-running after loading more sales keeps management's decisions
    instead of silently discarding them. It takes effect on the next run, and
    the response says so rather than implying the numbers have already moved.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        row = adjustments.record(
            session, user, version=version, plan=plan,
            level=request.level, node_code=request.node_code,
            material_code=request.material_code,
            adjustment_volume=request.adjustment_volume,
            reason=request.reason,
        )
        return {"adjustment": adjustments.to_dict(row),
                "applies_on_next_run": True}

    return _commit(
        session, user, http_request, resource="target_adjustment",
        audit_action=AuditAction.TARGET_ADJUSTMENT_SET,
        detail={"version_id": version_id, "level": request.level,
                "node_code": request.node_code,
                "adjustment_volume": request.adjustment_volume},
        work=work,
    )


@router.delete("/versions/{version_id}/adjustments/{adjustment_id}")
def delete_adjustment(
    version_id: int,
    adjustment_id: int,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    """Withdraw an adjustment so the next run allocates without it.

    The only delete in this module, and it is right here: an adjustment is a
    standing *instruction*, not a record of something that happened. Withdrawing
    it means "do not apply this next time", and the fact that it once existed
    survives in ``target_audit``, where that record belongs.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        removed = adjustments.remove(session, user, version=version, plan=plan,
                                     adjustment_id=adjustment_id)
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "TARGET_ADJUSTMENT_NOT_FOUND",
                        "message": "That adjustment was not found."},
            )
        return {"removed": True}

    return _commit(
        session, user, http_request, resource="target_adjustment",
        audit_action=AuditAction.TARGET_ADJUSTMENT_SET,
        detail={"version_id": version_id, "adjustment_id": adjustment_id,
                "removed": True},
        work=work,
    )


# ---------------------------------------------------------------------------
# Hierarchical review
# ---------------------------------------------------------------------------


@router.get("/versions/{version_id}/review")
def review_tree(
    version_id: int,
    material_code: str | None = Query(default=None, max_length=64),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """Every visible node of the allocation, with its own arithmetic.

    The tree is read from the **stored allocation**, whose rows name their own
    parent — a snapshot of the hierarchy as it stood when the run happened.
    Rebuilding it from the organisational masters would mean a territory moved
    to another region next month silently reshaping a target somebody has
    already approved.

    Scope narrows where the tree *starts*, not what the numbers inside it say. A
    regional manager sees their region as the root with its real figures; they
    do not see a country row carrying a narrowed total labelled "Country", which
    would be a figure that is neither the country's nor theirs.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return {
            "plan": plans.plan_to_dict(plan),
            "version": plans.version_to_dict(version),
            **review.tree(session, user, plan=plan, version_id=version_id,
                          material_code=material_code),
        }

    return _read(work)


# ---------------------------------------------------------------------------
# The approval matrix
# ---------------------------------------------------------------------------


#: The fields :func:`matrix.update` will act on. Named here so a client cannot
#: reach ``role`` or an unrelated attribute through ``fields_present``.
_MATRIX_FIELDS = frozenset({
    "hierarchy_level", "approval_sequence", "can_edit", "can_approve",
    "can_reject", "can_revise", "adjustment_limit_percent", "is_active",
})


class MatrixEntry(BaseModel):
    """One role's place in the chain. Every field but ``role`` is optional.

    Optional because the screen sends what it changed, and a partially loaded
    form must not be able to blank a column it never displayed.

    ``fields_present`` is what distinguishes "leave the sequence alone" from
    "put this role outside the chain". Both arrive as ``None`` over JSON, they
    mean opposite things, and without the list one silently becomes the other.
    """

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=32)
    hierarchy_level: str | None = Field(default=None, max_length=24)
    approval_sequence: int | None = Field(default=None, ge=1, le=99)
    can_edit: bool | None = None
    can_approve: bool | None = None
    can_reject: bool | None = None
    can_revise: bool | None = None
    adjustment_limit_percent: float | None = Field(default=None, ge=0, le=1000)
    is_active: bool | None = None
    reason: str | None = Field(default=None, max_length=500)
    fields_present: list[str] = Field(default_factory=list, max_length=16)


class MatrixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[MatrixEntry] = Field(min_length=1, max_length=32)


@router.get("/approval-matrix")
def approval_matrix(session: Session = Depends(get_session),
                    user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """The configured chain, plus the shipped default a reset would restore.

    Inactive rows are included. The screen has to show a deactivated role in
    order to offer switching it back on, and every *decision* asks
    ``MatrixRow.in_chain``, which excludes them anyway.
    """
    return _read(lambda: {
        "rows": [row.to_dict() for row in
                 sorted(matrix.load(session).values(),
                        key=lambda row: (row.approval_sequence is None,
                                         row.approval_sequence or 0, row.role))],
        "chain": [row.to_dict() for row in matrix.chain(session)],
        "levels": list(TargetLevel.ORDERED),
        "defaults": matrix.seeded_defaults(),
        "my_role": user.role,
    })


@router.put("/approval-matrix")
def update_approval_matrix(
    request: MatrixRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    """Reconfigure the chain. A role absent from the payload is left alone.

    Omission is not deletion: a half-loaded form must not be able to empty the
    workflow, and configuration with no rows is not a blank slate — it is a
    chain in which nobody can approve anything.
    """
    def work() -> dict[str, Any]:
        entries = [
            {
                "role": entry.role,
                **{field: getattr(entry, field)
                   for field in entry.fields_present if field in _MATRIX_FIELDS},
                **({"reason": entry.reason} if entry.reason else {}),
            }
            for entry in request.entries
        ]
        return {"chain": [row.to_dict()
                          for row in matrix.update(session, user, entries)]}

    return _commit(
        session, user, http_request, resource="target_approval_matrix",
        audit_action=AuditAction.TARGET_MATRIX_UPDATED,
        detail={"roles": [entry.role for entry in request.entries]},
        work=work,
    )


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


class RevisionRequest(BaseModel):
    """A request to change one node's figure.

    ``requested_volume`` is a **string**, deliberately. The browser sends what
    was typed and the backend decides what it means, so a value with a letter O
    in place of a zero is refused by name rather than silently coerced by JSON
    parsing that happens before any of this code runs.
    """

    model_config = ConfigDict(extra="forbid")

    level: str = Field(min_length=1, max_length=24)
    node_code: str = Field(min_length=1, max_length=64)
    material_code: str | None = Field(default=None, max_length=64)
    requested_volume: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=1000)


class RevisionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    approved_volume: str | None = Field(default=None, max_length=32)
    comment: str | None = Field(default=None, max_length=1000)


@router.get("/versions/{version_id}/revisions")
def list_revisions(
    version_id: int,
    open_only: bool = Query(default=False),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """Every revision raised against this version, newest first."""
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        names = review.node_names(session)
        rows = revisions.for_version(
            session, version_id,
            status=RevisionStatus.OPEN if open_only else None)
        return {
            "plan": plans.plan_to_dict(plan),
            "version": plans.version_to_dict(version),
            "rows": [revisions.to_dict(
                row, node_name=names.get((row.level, row.node_code)))
                for row in rows],
            "open_count": revisions.open_count(session, version_id),
            "revisable": version.status in revisions.REVISABLE,
        }

    return _read(work)


@router.post("/versions/{version_id}/revisions",
             status_code=status.HTTP_201_CREATED)
def create_revision(
    version_id: int,
    request: RevisionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_REVISE),
) -> dict[str, Any]:
    """Ask for a figure to be changed. Nothing moves until somebody decides.

    ``REVISE`` rather than ``EDIT``: asking is not changing, and the Sales
    Officer who knows the target is wrong is deliberately not the person who
    signs off on the correction.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        revision = revisions.request(
            session, user, plan=plan, version=version, level=request.level,
            node_code=request.node_code, material_code=request.material_code,
            requested_volume=request.requested_volume, reason=request.reason)
        return {"revision": revisions.to_dict(revision)}

    return _commit(
        session, user, http_request, resource=f"target_version:{version_id}",
        audit_action=AuditAction.TARGET_REVISION_REQUESTED,
        detail={"version_id": version_id, "level": request.level,
                "node_code": request.node_code},
        work=work,
    )


@router.post("/versions/{version_id}/revisions/{revision_id}/decision")
def decide_revision(
    version_id: int,
    revision_id: int,
    request: RevisionDecision,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_APPROVE),
) -> dict[str, Any]:
    """Grant or refuse one request, applying it to the allocation if granted.

    An approved change is funded by the node's siblings, so the country target
    does not move and the tree still reconciles the moment the decision lands.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        revision = revisions.decide(
            session, user, plan=plan, version=version, revision_id=revision_id,
            approve=request.approve, approved_volume=request.approved_volume,
            comment=request.comment)
        return {"revision": revisions.to_dict(revision),
                "open_count": revisions.open_count(session, version_id)}

    return _commit(
        session, user, http_request, resource=f"target_revision:{revision_id}",
        audit_action=AuditAction.TARGET_REVISION_DECIDED,
        detail={"version_id": version_id, "revision_id": revision_id,
                "approve": request.approve},
        work=work,
    )


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=1000)


class RejectionRequest(BaseModel):
    """A rejection carries a required reason; an approval's comment is optional.

    Asymmetric on purpose. A rejection with no reason tells its author the
    target is wrong and nothing about what to change, which is the least useful
    message this workflow can produce.
    """

    model_config = ConfigDict(extra="forbid")

    comment: str = Field(min_length=1, max_length=1000)


@router.get("/versions/{version_id}/approval")
def approval_state(version_id: int, session: Session = Depends(get_session),
                   user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """The chain, the log, and what final approval is still waiting on."""
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return {
            "plan": plans.plan_to_dict(plan),
            "version": plans.version_to_dict(version),
            **approvals.state(session, user, version_id=version_id),
        }

    return _read(work)


@router.post("/versions/{version_id}/submit")
def submit_for_approval(
    version_id: int,
    request: ApprovalRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    """Put an allocated target in front of the chain. ``EDIT``, not ``APPROVE``.

    Submitting is the planner saying the target is ready to be looked at, which
    is the same privilege as building it — and deliberately not the privilege of
    signing it off.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        approvals.submit(session, user, plan=plan, version=version,
                         comment=request.comment)
        return approvals.state(session, user, version_id=version_id)

    return _commit(
        session, user, http_request, resource=f"target_version:{version_id}",
        audit_action=AuditAction.TARGET_SUBMITTED,
        detail={"version_id": version_id}, work=work,
    )


@router.post("/versions/{version_id}/approve")
def approve_version(
    version_id: int,
    request: ApprovalRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_APPROVE),
) -> dict[str, Any]:
    """Record this role's approval, and advance the version if it was the last."""
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return approvals.approve(session, user, plan=plan, version=version,
                                 comment=request.comment)

    return _commit(
        session, user, http_request, resource=f"target_version:{version_id}",
        audit_action=AuditAction.TARGET_APPROVED,
        detail={"version_id": version_id}, work=work,
    )


@router.post("/versions/{version_id}/reject")
def reject_version(
    version_id: int,
    request: RejectionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_APPROVE),
) -> dict[str, Any]:
    """Send the whole version back to its author, with a required reason."""
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return approvals.reject(session, user, plan=plan, version=version,
                                comment=request.comment)

    return _commit(
        session, user, http_request, resource=f"target_version:{version_id}",
        audit_action=AuditAction.TARGET_REJECTED,
        detail={"version_id": version_id}, work=work,
    )


@router.post("/versions/{version_id}/send-back")
def send_version_back(
    version_id: int,
    request: RejectionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_APPROVE),
) -> dict[str, Any]:
    """Return an approved version to review. Approval is reversible until lock.

    Distinct from rejecting: the version goes back to the chain rather than back
    to its author, which is what a later approver spotting a problem after
    somebody else has signed actually wants.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return approvals.send_back(session, user, plan=plan, version=version,
                                   comment=request.comment)

    return _commit(
        session, user, http_request, resource=f"target_version:{version_id}",
        audit_action=AuditAction.TARGET_SENT_BACK,
        detail={"version_id": version_id}, work=work,
    )


@router.get("/my-approvals")
def my_approvals(session: Session = Depends(get_session),
                 user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """Everything waiting on this person, and nothing waiting on anybody else.

    Two lists rather than one merged queue: signing off on a whole target and
    granting one figure are different acts, and a shared shape would lose the
    distinction. A role outside the chain gets empty lists and an explanation
    rather than an error — an administrator legitimately approves nothing.
    """
    return _read(lambda: approvals.queue(session, user))


# ---------------------------------------------------------------------------
# Locking, and the business audit trail
# ---------------------------------------------------------------------------


class LockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=1000)


@router.get("/versions/{version_id}/lock")
def lock_state(version_id: int, session: Session = Depends(get_session),
               user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """Whether this version can be locked, and what is stopping it.

    Read-only and cheap enough for every page load, so a planner sees what to
    fix *before* pressing the button rather than after — the same reason the
    allocation readiness gate runs in the request.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return {
            "plan": plans.plan_to_dict(plan),
            "version": plans.version_to_dict(version),
            **target_lock.state(session, plan=plan, version=version),
        }

    return _read(work)


@router.post("/versions/{version_id}/lock")
def lock_version(
    version_id: int,
    request: LockRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_APPROVE),
) -> dict[str, Any]:
    """Write the agreed allocation into ``fact_target``. One transaction.

    ``APPROVE`` rather than ``EDIT``: locking is the last act of the approval
    chain, not an act of planning, and it is the point after which a corrected
    transfer price can no longer change what was agreed.

    The response says what was written — inserted, updated and voided — rather
    than a bare success, because "the target is locked" and "1,872 rows now
    stand" are different amounts of reassurance.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return target_lock.lock(session, user, plan=plan, version=version,
                                comment=request.comment)

    return _commit(
        session, user, http_request, resource=f"target_version:{version_id}",
        audit_action=AuditAction.TARGET_LOCKED,
        detail={"version_id": version_id}, work=work,
    )


@router.get("/audit")
def audit_trail(
    plan_id: int | None = Query(default=None, ge=1),
    version_id: int | None = Query(default=None, ge=1),
    action: str | None = Query(default=None, max_length=48),
    actor: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """The business trail: what a figure was, what it became, and why.

    Separate from the security log an administrator reads beside a login. This
    one is a planner's record of the decisions taken on a target, filtered on
    the server because a plan's trail grows without bound and shipping all of it
    so the browser can hide most of it would get slower exactly as the record
    gets more valuable.
    """
    return _read(lambda: target_audit.trail(
        session, plan_id=plan_id, version_id=version_id, action=action,
        actor=actor, limit=limit, offset=offset))


# ---------------------------------------------------------------------------
# Comparison and the dashboard
# ---------------------------------------------------------------------------


@router.get("/plans/{plan_id}/compare-options")
def compare_options(plan_id: int, session: Session = Depends(get_session),
                    user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """The versions of one plan that can be compared, newest first.

    A version with no allocation is listed and marked rather than hidden: a
    reader looking for V2 and not finding it would assume the list was broken,
    where an entry saying "not allocated" answers them.
    """
    def work() -> dict[str, Any]:
        plan = plans.get_plan(session, plan_id)
        return {"plan": plans.plan_to_dict(plan),
                "versions": compare.options(session, plan_id)}

    return _read(work)


@router.get("/versions/{version_id}/compare")
def compare_versions(
    version_id: int,
    base_version_id: int = Query(ge=1),
    material_code: str | None = Query(default=None, max_length=64),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """What moved between two versions of one plan.

    Only two versions of the *same* plan. Two plans have different scopes,
    materials and hierarchies, so a difference between them would be two
    unrelated targets subtracted from each other — refused by name rather than
    rendered as a table of apparent movements.
    """
    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return {
            "plan": plans.plan_to_dict(plan),
            **compare.compare(session, user, plan=plan,
                              base_version_id=base_version_id,
                              version_id=version_id,
                              material_code=material_code),
        }

    return _read(work)


@router.get("/dashboard")
def target_dashboard(
    financial_year: str | None = Query(default=None, max_length=32),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """Where every plan has got to, and what is waiting on somebody.

    Counts of workflow state and figures somebody typed — never a scoped
    aggregate, because one headline volume would mean something different to
    every reader and there would be no honest label for it. Achievement against
    a locked target is the Target page's question and is deliberately absent.
    """
    return _read(lambda: dashboard.summary(session, user,
                                           financial_year=financial_year))


# ---------------------------------------------------------------------------
# Bulk country-target upload
# ---------------------------------------------------------------------------


@router.get("/versions/{version_id}/country-target/template")
def country_target_template(
    version_id: int,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> Response:
    """A CSV of this plan's own materials with the version's current figures.

    Pre-filled rather than blank: the commonest use is *adjusting* a target
    rather than entering one from nothing, and a template listing only materials
    of the plan's company makes the commonest rejection impossible to hit by
    accident. A material with no figure yet is left blank, never zero.
    """
    def work() -> Response:
        version, plan = _version_and_plan(session, version_id)
        content, name = bulk.template(session, plan, version)
        return Response(
            content=content, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return _read(work)


@router.post("/versions/{version_id}/country-target/preview",
             status_code=status.HTTP_200_OK)
async def preview_country_target_upload(
    version_id: int,
    file: UploadFile = File(..., description="Excel (.xlsx) or CSV (.csv) file."),
    sheet_name: str | None = Form(None),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_UPLOAD),
) -> dict[str, Any]:
    """Read the file and say what applying it would do. Writes nothing.

    **200, not 202**: a country target is one row per material — hundreds, not
    the hundreds of thousands a transaction import produces — so this finishes
    before the response does and a background job would only add a status to
    poll for.

    The staged file's token comes back and is what ``apply`` takes. The bytes
    are re-read and re-validated there rather than trusted from here: a version
    can be approved, or a material retired, in between.
    """
    try:
        stored = await run_in_threadpool(
            upload_files.store, file.file, file.filename, file.content_type)
    except upload_files.UploadFileError as exc:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    finally:
        await file.close()

    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        preview = bulk.read(stored, session, plan=plan, version=version,
                            sheet_name=sheet_name)
        return {
            "plan": plans.plan_to_dict(plan),
            "version": plans.version_to_dict(version),
            "upload_token": stored.path.name,
            "file_name": stored.original_name,
            "editable": version.status in TargetStatus.EDITABLE,
            **preview.to_dict(),
        }

    try:
        return await run_in_threadpool(_read, work)
    except Exception:
        # The staged file is discarded on any failure: a file that could not be
        # read is one nothing will ever ask for again, and leaving it on disk
        # would accumulate bytes nobody can account for.
        upload_files.discard(stored.path)
        raise


class ApplyUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The staged file's name, from the preview. A name rather than a path: the
    #: server resolves it inside the upload directory, so nothing a caller sends
    #: can reach a file outside it.
    upload_token: str = Field(min_length=1, max_length=128)
    sheet_name: str | None = Field(default=None, max_length=64)


@router.post("/versions/{version_id}/country-target/apply")
def apply_country_target_upload(
    version_id: int,
    request: ApplyUploadRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_UPLOAD),
) -> dict[str, Any]:
    """Apply a previewed file. All-or-nothing, through the ordinary write path.

    Every accepted row goes through ``country.set_lines``, so an uploaded figure
    and a typed one are validated, audited and versioned by exactly the same
    code — an upload is the same act performed from a spreadsheet, not a second
    way in.
    """
    stored = _staged_upload(request.upload_token)

    def work() -> dict[str, Any]:
        version, plan = _version_and_plan(session, version_id)
        return bulk.apply(session, user, plan=plan, version=version,
                          stored=stored, sheet_name=request.sheet_name)

    result = _commit(
        session, user, http_request, resource=f"target_version:{version_id}",
        audit_action=AuditAction.TARGET_COUNTRY_TARGET_EDITED,
        detail={"version_id": version_id, "upload": stored.original_name},
        work=work,
    )
    # Discarded only once the write has committed. Keeping it until then means a
    # refused apply can be retried against the same bytes after the master data
    # is corrected, without asking for the file again.
    upload_files.discard(stored.path)
    return result


def _staged_upload(token: str) -> upload_files.StoredUpload:
    """Resolve a preview's token to the file it staged.

    The token is a **name**, never a path, and it is resolved inside the upload
    directory and checked to be there — so ``../`` in a token reaches nothing.
    """
    from pathlib import Path

    directory = upload_files.upload_dir().resolve()
    candidate = (directory / Path(token).name).resolve()
    if candidate.parent != directory or not candidate.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error_code": "TARGET_UPLOAD_NOT_STAGED",
                    "message": ("That upload is no longer staged. Upload the "
                                "file again to preview it.")},
        )
    return upload_files.StoredUpload(
        path=candidate, original_name=candidate.name,
        extension=candidate.suffix.lower(), size=candidate.stat().st_size,
        content_type=None,
    )
