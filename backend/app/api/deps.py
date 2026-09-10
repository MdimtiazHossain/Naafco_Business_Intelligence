"""Shared API dependencies and error handling.

Security posture for every endpoint:

* database credentials come from the environment and are never returned;
* internal exceptions are logged server-side and answered with a generic
  message — no SQL, no driver text, no stack trace reaches the client;
* filters are bound parameters against whitelisted columns, never string SQL.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database.connection import get_engine
from ..reporting.service import DEFAULT_LIMIT, MAX_LIMIT, ReportFilters

logger = logging.getLogger("app.api")

GENERIC_ERROR = "The request could not be completed. See the server log for details."


def get_session() -> Iterator[Session]:
    """One session per request, always closed."""
    session = Session(bind=get_engine(), expire_on_commit=False, future=True)
    try:
        yield session
    finally:
        session.close()


#: Legacy identity header from Phase 3.
#:
#: Convenient behind a trusted gateway and in development, but it is not
#: authentication — anyone who can reach the API can claim any username. It is
#: honoured only while ``Settings.allow_header_auth`` is true, which defaults to
#: false in production. The ``Authorization: Bearer`` token is the real path.
USER_HEADER = "X-User"

UNAUTHENTICATED = "Not authenticated."


def get_current_user(
    session: Session = Depends(get_session),
    authorization: str | None = Header(None),
    x_user: str | None = Header(None, alias=USER_HEADER),
):
    """Resolve the caller to a ``UserContext``, or reject the request.

    A verified bearer token wins. The legacy header is accepted only when
    explicitly allowed. Either way the username is just an identity: the role
    and data scope are re-read from the database, so a stale or tampered token
    can never widen access.
    """
    from ..ai.permission_filter import load_user_context
    from ..auth.security import decode_access_token

    username: str | None = None

    if authorization and authorization.lower().startswith("bearer "):
        payload = decode_access_token(authorization.split(None, 1)[1].strip())
        if payload is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Session expired. Please sign in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        username = payload.username
    elif x_user and get_settings().allow_header_auth:
        username = x_user

    if not username:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, UNAUTHENTICATED,
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = load_user_context(session, username)
    if user is None:
        # Deliberately identical wording to the missing-credential case: this
        # must not become a way to discover which usernames exist.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Unknown or inactive user.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_admin(user=Depends(get_current_user),
                  session: Session = Depends(get_session)):
    """Gate admin endpoints. Enforced here in the backend, not in the UI.

    Two checks, not one. The role check is the hard ceiling — administration can
    only ever belong to an administrator. The section check is on top of it, so
    an administrator whose ``admin`` section has been explicitly denied is
    refused as well: the permission table is the whole truth, and nothing routes
    around it.
    """
    from ..auth.permissions import FORBIDDEN_MESSAGE, has_section
    from ..database.models_ai import Role
    from ..security.sections import SectionKey

    if user.role not in Role.ADMIN_ROLES or not has_section(
        session, user, SectionKey.ADMIN
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_MESSAGE)
    return user


def enforce_report_scope(session: Session, user, filters: ReportFilters,
                         view: str) -> ReportFilters:
    """Constrain a Phase 2 report's filters to the caller's data scope.

    ``ReportFilters`` predates the scope model and holds one code per level,
    while a scope holds a list. So this does the two things that are always
    sound:

    * every organisational code the caller *did* supply is checked against their
      scope, and an out-of-scope code is a 403 — never a silently empty result;
    * a scoped caller who supplied no organisational filter has their own scope
      applied, so an unfiltered request cannot return the whole company.

    A caller whose scope spans several codes at a level cannot be expressed in a
    single-valued filter, so they are pointed at ``/api/pages/*``, which carries
    the full multi-code scope. Refusing is the only safe answer: guessing one of
    their codes would silently hide the rest.

    **``view`` is required, and it closes a hole that predates the plant
    scope.** ``_apply_filters`` drops a filter naming a column the view lacks,
    and these endpoints do not all read the wide detail views the pages do:
    ``vw_target_vs_actual`` carries ``region_code`` and nothing else below it,
    and ``vw_daily_sales`` stops at territory. So the scope this function
    carefully applied was then discarded further down, and an area- or
    territory-scoped caller was served the whole country's targets. Asking the
    view what it can express — and refusing under its declared policy — is the
    same check the tool path makes, so the two surfaces can no longer disagree
    about who may see what.
    """
    import dataclasses

    from ..ai import queries as q
    from ..ai.exceptions import PermissionDeniedError
    from ..ai.permission_filter import FILTER_FIELD_BY_LEVEL, PermissionFilter

    permissions = PermissionFilter(session, user)
    if user.is_unrestricted:
        return filters

    if not user.data_scope:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account has no data scope assigned, so no business data can be "
            "returned. Please contact your administrator.",
        )

    table = q.view(session, view)
    try:
        permissions.assert_scope_is_honourable(
            table, q.scope_policy(view), "this report")
    except PermissionDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.user_message) from exc

    supplied = [
        (level, getattr(filters, level))
        for level in FILTER_FIELD_BY_LEVEL
        if getattr(filters, level, None)
    ]
    for level, code in supplied:
        if not permissions.is_within_scope(level, code):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"You don't have permission to access data for {code}. Your access "
                f"covers {user.describe_scope()}.",
            )
    if supplied:
        return filters

    expressible = {field.name for field in dataclasses.fields(filters)}
    # Only a level this view can actually filter on. Taking the deepest scope
    # level regardless would pin ``region_code`` on a caller scoped to a region
    # *and* a plant and then have the stock view discard it, leaving their plant
    # scope unapplied — a scope silently dropped by the very function that
    # exists to apply it.
    for level in user.scope_levels():          # deepest first, within each chain
        if level not in expressible or level not in table.c:
            continue
        codes = user.data_scope[level]
        if len(codes) != 1:
            break
        return dataclasses.replace(filters, **{level: codes[0]})

    if not any(level in table.c for level in user.scope_levels()):
        # Nothing of this scope is expressible here and the view's policy is to
        # disclose anyway — material stock, which is not held below company and
        # has no finer figure to withhold. Unchanged behaviour, now reached by a
        # stated rule instead of by the filter quietly evaporating downstream.
        return filters

    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "Your data scope cannot be expressed as a single filter on this endpoint. "
        "Use the report pages, which carry your full scope, or supply an explicit "
        "filter within your scope.",
    )


def internal_error(exc: Exception, context: str) -> HTTPException:
    """Log the real cause, return an opaque error to the caller.

    An :class:`AgentError` is a *business* outcome, not a fault, so it keeps its
    own status and user-safe message instead of becoming a 500.
    """
    from ..ai.exceptions import (
        AgentError,
        AmbiguousEntityError,
        DateResolutionError,
        EntityNotFoundError,
        NoDataError,
        PermissionDeniedError,
    )

    if isinstance(exc, PermissionDeniedError):
        # A scoped user reaching for data outside their scope is a 403, and the
        # message explains what they *can* see.
        logger.info("permission denied in %s: %s", context, exc.user_message)
        return HTTPException(status.HTTP_403_FORBIDDEN, exc.user_message)
    if isinstance(exc, NoDataError):
        return HTTPException(status.HTTP_404_NOT_FOUND, exc.user_message)
    if isinstance(exc, (AmbiguousEntityError, EntityNotFoundError,
                        DateResolutionError)):
        return HTTPException(status.HTTP_400_BAD_REQUEST, exc.user_message)
    if isinstance(exc, AgentError):
        logger.warning("%s failed: %s", context, exc.code)
        return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, exc.user_message)

    logger.exception("%s failed: %s", context, exc)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=GENERIC_ERROR
    )


def report_filters(
    date_from: dt.date | None = Query(None, description="Inclusive start date."),
    date_to: dt.date | None = Query(None, description="Inclusive end date."),
    source_system: str | None = Query(None, description="e.g. DEMO, SAP, SALES_APP."),
    company_code: str | None = None,
    bu_code: str | None = None,
    sales_line_code: str | None = None,
    zone_code: str | None = None,
    region_code: str | None = None,
    area_code: str | None = None,
    unit_code: str | None = None,
    territory_code: str | None = None,
    sub_territory_code: str | None = None,
    plant_code: str | None = Query(None, description="One plant, for stock."),
    sku_code: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    customer_code: str | None = None,
    batch_code: str | None = Query(None, description="One production batch."),
    financial_year: str | None = Query(None, description="e.g. 'FY 2026-27'."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> ReportFilters:
    """Parse and bound the standard reporting filters.

    No volume-unit filter: a transaction line records one Total Volume and no
    unit of measure, so there is no subset of it to select.
    """
    return ReportFilters(
        date_from=date_from,
        date_to=date_to,
        source_system=source_system,
        company_code=company_code,
        bu_code=bu_code,
        sales_line_code=sales_line_code,
        zone_code=zone_code,
        region_code=region_code,
        area_code=area_code,
        unit_code=unit_code,
        territory_code=territory_code,
        sub_territory_code=sub_territory_code,
        plant_code=plant_code,
        sku_code=sku_code,
        category=category,
        brand=brand,
        customer_code=customer_code,
        batch_code=batch_code,
        financial_year=financial_year,
        limit=limit,
        offset=offset,
    )


SessionDep = Depends(get_session)
FiltersDep = Depends(report_filters)
