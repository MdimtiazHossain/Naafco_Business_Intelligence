"""The Data Management API: master records and transaction records.

Three gates open before any handler here touches data, and all three are
server-side:

1. **Section** — ``master_data`` or ``transaction_data``. Transactional types
   additionally require the reporting section for that data type, so a user
   denied Stock cannot read stock rows through the management door.
2. **Action** — VIEW / CREATE / EDIT / DELETE / EXPORT, resolved per user per
   section. The frontend hides what it would be refused, but hiding is never
   what stops it.
3. **Data scope** — applied to the query, and re-checked against the individual
   record on every write, so addressing a record directly is not a way past the
   table's filter.

URL shape follows the existing convention — a resource prefix, then the entity,
then the record — and matches what the specification asked for:
``/api/master/{entity}``, ``/api/transactions/{type}``.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..ai.schemas import ScopeFilters
from ..auth import audit
from ..auth.permissions import (
    FORBIDDEN_MESSAGE,
    allowed_actions,
    can,
    has_section,
    require_action_or_403,
)
from ..database.models_ai import AuditAction
from ..datamgmt import catalogue as cat
from ..datamgmt import dependencies, export, history, query, service
from ..datamgmt.catalogue import ManagedEntity
from ..datamgmt.query import ListRequest, QueryProblem
from ..datamgmt.validation import ValidationFailed
from ..security.sections import Action, SectionKey
from .deps import get_current_user, get_session, internal_error
from .routes_dashboard import scope_filters

logger = logging.getLogger("app.api.data_management")

router = APIRouter(tags=["data-management"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class RecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Only the fields being set. A partial edit leaves the rest untouched.
    values: dict[str, Any]
    reason: str | None = Field(None, max_length=500)


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(None, max_length=500)


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., max_length=32)


class BulkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    codes: list[str] = Field(..., min_length=1)
    reason: str | None = Field(None, max_length=500)


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _master_or_404(key: str) -> ManagedEntity:
    try:
        return cat.get_master(key)
    except cat.UnknownEntity as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


def _transaction_or_404(key: str) -> ManagedEntity:
    try:
        return cat.get_transaction(key)
    except cat.UnknownEntity as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


def _authorise(session: Session, user: UserContext, entity: ManagedEntity,
               action: str, request: Request | None = None) -> None:
    """Both gates for one operation on one entity.

    The reporting-section check comes first for a transactional type: a user who
    may not see Stock at all should be refused before the management section is
    even consulted, and should get the same answer here as on the Stock page.
    """
    if entity.data_type is not None:
        report = cat.report_section(entity.data_type)
        if not has_section(session, user, report):
            raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_MESSAGE)
    require_action_or_403(session, user, entity.section, action, request)


def _list_request(*, search: str | None, sort_by: str | None, sort_dir: str,
                  page: int, page_size: int, include_deleted: bool,
                  include_voided: bool, filters: dict[str, str],
                  codes: str | None = None,
                  date_from: dt.date | None = None,
                  date_to: dt.date | None = None,
                  business: ScopeFilters | None = None) -> ListRequest:
    return ListRequest(
        search=search, filters=filters, sort_by=sort_by, sort_dir=sort_dir,
        page=page, page_size=page_size, include_deleted=include_deleted,
        include_voided=include_voided, date_from=date_from, date_to=date_to,
        business_filters=business,
        codes=tuple(c for c in (codes or "").split(",") if c.strip()),
    )


def _entity_filters(entity: ManagedEntity, raw: dict[str, Any]) -> dict[str, str]:
    """Pull the entity's own filter parameters out of the query string.

    FastAPI cannot declare a parameter per field of a catalogue that is built at
    import time, so filters arrive as ``filter.<field>=<value>`` and are matched
    against the entity's declared fields here. A name that is not a field is
    refused rather than ignored, because silently dropping a filter would show
    the user more rows than they asked for.
    """
    known = {f.name for f in entity.fields}
    if entity.filter_fields:
        known &= set(entity.filter_fields)
    filters: dict[str, str] = {}
    unknown: list[str] = []
    for key, value in raw.items():
        if not key.startswith("filter.") or value in (None, ""):
            continue
        name = key.removeprefix("filter.")
        if name not in known:
            unknown.append(name)
            continue
        filters[name] = str(value)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"'{entity.label}' cannot be filtered by {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(known))}.",
        )
    return filters


def _permissions(session: Session, user: UserContext,
                 entity: ManagedEntity) -> dict[str, bool]:
    """What this caller may do here, so the table renders the right actions."""
    return allowed_actions(session, user, entity.section)


def _handle(exc: Exception, context: str) -> HTTPException:
    """Translate a service outcome into the right status code.

    Each of these is a *business* answer rather than a fault, so each keeps its
    own status and its own message instead of collapsing into a 500.
    """
    if isinstance(exc, ValidationFailed):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                             exc.to_dict())
    if isinstance(exc, service.RecordNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, service.ScopeDenied):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, service.OperationRefused):
        # 409: the request is well-formed and permitted, but conflicts with the
        # record's current state or with what depends on it.
        detail: Any = exc.message
        if exc.dependants is not None:
            detail = {"detail": exc.message,
                      "dependants": exc.dependants.to_dict()}
        return HTTPException(status.HTTP_409_CONFLICT, detail)
    if isinstance(exc, QueryProblem):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    if isinstance(exc, export.ExportTooLarge):
        return HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc))
    if isinstance(exc, HTTPException):
        return exc
    return internal_error(exc, context)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@router.get("/api/data-management/catalogue")
def data_catalogue(session: Session = Depends(get_session),
                   user: UserContext = Depends(get_current_user),
                   ) -> dict[str, Any]:
    """Every manageable entity, with what this caller may do to each.

    One call builds the whole Data Management menu, so the frontend never has to
    guess which entities exist or which buttons to draw.
    """
    payload = cat.catalogue()
    master_actions = _actions_for(session, user, SectionKey.MASTER_DATA)
    transaction_actions = _actions_for(session, user, SectionKey.TRANSACTION_DATA)

    for group in payload["groups"]:
        is_master = group["key"] == cat.MASTER
        actions = master_actions if is_master else transaction_actions
        group["permissions"] = actions
        group["accessible"] = actions.get(Action.VIEW, False)
        visible = []
        for entry in group["entities"]:
            entity_actions = dict(actions)
            if not is_master:
                # A transactional type is only reachable if its reporting
                # section is held as well.
                report = cat.report_section(entry["data_type"])
                if not has_section(session, user, report):
                    continue
                entry["report_section"] = report
            entry["permissions"] = entity_actions
            visible.append(entry)
        group["entities"] = visible
    return payload


def _actions_for(session: Session, user: UserContext,
                 section: str) -> dict[str, bool]:
    if not has_section(session, user, section):
        return {action: False for action in (
            Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE, Action.EXPORT
        )}
    return allowed_actions(session, user, section)


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


@router.get("/api/master/{entity_key}")
def list_master_records(
    entity_key: str,
    request: Request,
    search: str | None = Query(None, max_length=200),
    sort_by: str | None = Query(None, max_length=64),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(query.DEFAULT_PAGE_SIZE, ge=1,
                           le=query.MAX_PAGE_SIZE),
    include_deleted: bool = Query(False),
    codes: str | None = Query(None, description="Comma-separated codes."),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """One page of a master-data table, scoped, searched, filtered and sorted."""
    entity = _master_or_404(entity_key)
    _authorise(session, user, entity, Action.VIEW, request)

    filters = _entity_filters(entity, dict(request.query_params))
    try:
        result = query.list_master(session, user, entity, _list_request(
            search=search, sort_by=sort_by, sort_dir=sort_dir, page=page,
            page_size=page_size, include_deleted=include_deleted,
            include_voided=False, filters=filters, codes=codes,
        ))
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc, f"{entity_key} list") from exc

    audit.record(session, action=AuditAction.VIEW_REPORT, user_id=user.user_id,
                 username=user.username, resource=f"master:{entity_key}",
                 ip_address=audit.client_ip(request),
                 detail={"page": page, "search": search})
    session.commit()

    return {
        "entity": entity.to_dict(),
        "permissions": _permissions(session, user, entity),
        **result.to_dict(),
    }


@router.get("/api/master/{entity_key}/{code}")
def get_master_record(
    entity_key: str,
    code: str,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """One master record, with where it sits, what it is worth and its history."""
    entity = _master_or_404(entity_key)
    _authorise(session, user, entity, Action.VIEW, request)

    try:
        record = service.load_master(session, user, entity, code)
        changes, total = history.for_record(session, entity, code, limit=25)
        payload = {
            "entity": entity.to_dict(),
            "permissions": _permissions(session, user, entity),
            "record": query.master_row(entity, record),
            "hierarchy": _hierarchy_of(session, entity, code),
            "location": _location_of(session, entity, code),
            "dependants": dependencies.for_master(session, entity, code).to_dict(),
            "history": changes,
            "history_total": total,
            "last_change": history.summarise(session, entity, code),
        }
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc, f"{entity_key} detail") from exc
    return payload


def _hierarchy_of(session: Session, entity: ManagedEntity,
                  code: str) -> dict[str, str] | None:
    """The record's ancestry, for the Hierarchy panel on the detail page."""
    if entity.scope_level is None:
        return None
    from ..etl.mapping import MasterDataIndex

    chain = MasterDataIndex(session).ancestors_of(entity.scope_level, code)
    return {level: value for level, value in chain.items() if value}


def _location_of(session: Session, entity: ManagedEntity,
                 code: str) -> dict[str, Any] | None:
    """The record's coordinate, so the detail page can offer "View on map"."""
    if not entity.supports_geo:
        return None
    from sqlalchemy import select

    from ..database.models_map import MapEntityLocation

    entity_type = entity.fact_scope_type or (entity.scope_level or "").removesuffix(
        "_code"
    )
    row = session.execute(
        select(MapEntityLocation).where(
            MapEntityLocation.entity_type == entity_type,
            MapEntityLocation.entity_code == code,
        )
    ).scalar_one_or_none()
    if row is None:
        return {"entity_type": entity_type, "latitude": None, "longitude": None}
    return {
        "entity_type": entity_type,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "source": row.source,
    }


@router.get("/api/master/{entity_key}/{code}/history")
def master_history(
    entity_key: str,
    code: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    entity = _master_or_404(entity_key)
    _authorise(session, user, entity, Action.VIEW, request)
    try:
        service.load_master(session, user, entity, code)
        changes, total = history.for_record(session, entity, code, limit=limit,
                                            offset=offset)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc, f"{entity_key} history") from exc
    return {"history": changes, "total": total, "limit": limit, "offset": offset}


@router.post("/api/master/{entity_key}", status_code=status.HTTP_201_CREATED)
def create_master_record(
    entity_key: str,
    body: RecordRequest,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    entity = _master_or_404(entity_key)
    _authorise(session, user, entity, Action.CREATE, request)
    try:
        result = service.create_master(session, user, entity, body.values,
                                       ip_address=audit.client_ip(request))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _handle(exc, f"{entity_key} create") from exc
    return result.to_dict()


@router.put("/api/master/{entity_key}/{code}")
def update_master_record(
    entity_key: str,
    code: str,
    body: RecordRequest,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    entity = _master_or_404(entity_key)
    _authorise(session, user, entity, Action.EDIT, request)
    try:
        result = service.update_master(session, user, entity, code, body.values,
                                       reason=body.reason,
                                       ip_address=audit.client_ip(request))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _handle(exc, f"{entity_key} update") from exc
    return result.to_dict()


@router.put("/api/master/{entity_key}/{code}/status")
def set_master_status(
    entity_key: str,
    code: str,
    body: StatusRequest,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Activate or deactivate — the inline edit the table offers on Status."""
    entity = _master_or_404(entity_key)
    _authorise(session, user, entity, Action.EDIT, request)
    try:
        result = service.set_status(session, user, entity, code, body.status,
                                    ip_address=audit.client_ip(request))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _handle(exc, f"{entity_key} status") from exc
    return result.to_dict()


@router.delete("/api/master/{entity_key}/{code}")
def delete_master_record(
    entity_key: str,
    code: str,
    request: Request,
    reason: str | None = Query(None, max_length=500),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Retire a master record. The row is flagged, never removed."""
    entity = _master_or_404(entity_key)
    _authorise(session, user, entity, Action.DELETE, request)
    try:
        result = service.delete_master(session, user, entity, code,
                                       reason=reason,
                                       ip_address=audit.client_ip(request))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _handle(exc, f"{entity_key} delete") from exc
    return result.to_dict()


@router.post("/api/master/{entity_key}/{code}/restore")
def restore_master_record(
    entity_key: str,
    code: str,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    entity = _master_or_404(entity_key)
    # Restoring is undoing a delete, so it is the delete permission that governs
    # it: anyone who can retire a record can put it back, and nobody else can.
    _authorise(session, user, entity, Action.DELETE, request)
    try:
        result = service.restore_master(session, user, entity, code,
                                        ip_address=audit.client_ip(request))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _handle(exc, f"{entity_key} restore") from exc
    return result.to_dict()


@router.get("/api/master/{entity_key}/{code}/dependants")
def master_dependants(
    entity_key: str,
    code: str,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """What references this record — read by the delete confirmation."""
    entity = _master_or_404(entity_key)
    _authorise(session, user, entity, Action.VIEW, request)
    try:
        service.load_master(session, user, entity, code)
        return dependencies.for_master(session, entity, code).to_dict()
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc, f"{entity_key} dependants") from exc


@router.post("/api/master/{entity_key}/bulk")
def bulk_master_records(
    entity_key: str,
    body: BulkRequest,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Apply one action to a selection.

    The permission required depends on the action, not on the endpoint: a bulk
    deactivate needs EDIT, a bulk delete needs DELETE. Gating the whole route on
    the weaker of the two would make bulk a way round the stronger.
    """
    entity = _master_or_404(entity_key)
    needed = (Action.DELETE if body.action in (service.BULK_DELETE,
                                               service.BULK_RESTORE)
              else Action.EDIT)
    _authorise(session, user, entity, needed, request)

    try:
        outcome = service.bulk_master(session, user, entity, body.action,
                                      body.codes, reason=body.reason,
                                      ip_address=audit.client_ip(request))
        audit.record(session, action=AuditAction.RECORD_BULK_CHANGE,
                     user_id=user.user_id, username=user.username,
                     resource=f"master:{entity_key}",
                     ip_address=audit.client_ip(request),
                     detail={"action": body.action,
                             "requested": len(body.codes),
                             "succeeded": len(outcome.succeeded),
                             "failed": len(outcome.failed)})
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _handle(exc, f"{entity_key} bulk") from exc
    return outcome.to_dict()


@router.get("/api/master/{entity_key}/export/{fmt}")
def export_master(
    entity_key: str,
    fmt: str,
    request: Request,
    search: str | None = Query(None, max_length=200),
    sort_by: str | None = Query(None, max_length=64),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    include_deleted: bool = Query(False),
    codes: str | None = Query(None),
    columns: str | None = Query(None, description="Comma-separated columns."),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> Response:
    """Export exactly what the caller's filters select — nothing wider."""
    entity = _master_or_404(entity_key)
    _authorise(session, user, entity, Action.EXPORT, request)
    filters = _entity_filters(entity, dict(request.query_params))
    return _export(session, user, entity, fmt, request, _list_request(
        search=search, sort_by=sort_by, sort_dir=sort_dir, page=1,
        page_size=export.CHUNK, include_deleted=include_deleted,
        include_voided=False, filters=filters, codes=codes,
    ), columns)


# ---------------------------------------------------------------------------
# Transaction data
# ---------------------------------------------------------------------------


@router.get("/api/transactions/{data_type}")
def list_transaction_records(
    data_type: str,
    request: Request,
    search: str | None = Query(None, max_length=200),
    sort_by: str | None = Query(None, max_length=64),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(query.DEFAULT_PAGE_SIZE, ge=1,
                           le=query.MAX_PAGE_SIZE),
    include_voided: bool = Query(False),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    codes: str | None = Query(None),
    business: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """One page of a transaction table."""
    entity = _transaction_or_404(data_type)
    _authorise(session, user, entity, Action.VIEW, request)

    filters = _entity_filters(entity, dict(request.query_params))
    try:
        result = query.list_transactions(session, user, entity, _list_request(
            search=search, sort_by=sort_by, sort_dir=sort_dir, page=page,
            page_size=page_size, include_deleted=False,
            include_voided=include_voided, filters=filters, codes=codes,
            date_from=date_from, date_to=date_to, business=business,
        ))
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc, f"{data_type} transaction list") from exc

    audit.record(session, action=AuditAction.VIEW_REPORT, user_id=user.user_id,
                 username=user.username, resource=f"transactions:{data_type}",
                 ip_address=audit.client_ip(request),
                 detail={"page": page, "search": search,
                         "include_voided": include_voided})
    session.commit()

    return {
        "entity": entity.to_dict(),
        "permissions": _permissions(session, user, entity),
        **result.to_dict(),
    }


@router.get("/api/transactions/{data_type}/{record_id}")
def get_transaction_record(
    data_type: str,
    record_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    entity = _transaction_or_404(data_type)
    _authorise(session, user, entity, Action.VIEW, request)
    try:
        record = service.load_transaction(session, user, entity, record_id)
        changes, total = history.for_record(session, entity, str(record_id),
                                            limit=25)
        return {
            "entity": entity.to_dict(),
            "permissions": _permissions(session, user, entity),
            "record": service.transaction_row(entity, record),
            "dependants": dependencies.for_transaction(
                session, entity, record).to_dict(),
            "history": changes,
            "history_total": total,
            "last_change": history.summarise(session, entity, str(record_id)),
        }
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc, f"{data_type} detail") from exc


@router.get("/api/transactions/{data_type}/{record_id}/history")
def transaction_history(
    data_type: str,
    record_id: int,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    entity = _transaction_or_404(data_type)
    _authorise(session, user, entity, Action.VIEW, request)
    try:
        service.load_transaction(session, user, entity, record_id)
        changes, total = history.for_record(session, entity, str(record_id),
                                            limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc, f"{data_type} history") from exc
    return {"history": changes, "total": total, "limit": limit, "offset": offset}


@router.put("/api/transactions/{data_type}/{record_id}")
def update_transaction_record(
    data_type: str,
    record_id: int,
    body: RecordRequest,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Correct a transaction's measures."""
    entity = _transaction_or_404(data_type)
    _authorise(session, user, entity, Action.EDIT, request)
    try:
        result = service.update_transaction(
            session, user, entity, record_id, body.values, reason=body.reason,
            ip_address=audit.client_ip(request),
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _handle(exc, f"{data_type} update") from exc
    return result.to_dict()


@router.delete("/api/transactions/{data_type}/{record_id}")
def void_transaction_record(
    data_type: str,
    record_id: int,
    request: Request,
    # A reason is required, and carried as a query parameter rather than a body:
    # a DELETE with a body is legal but unevenly supported, and a reversal that
    # silently lost its explanation would be worse than a clumsy URL.
    reason: str = Query(..., min_length=3, max_length=500),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Void a transaction. Never a physical delete, and never with dependants."""
    entity = _transaction_or_404(data_type)
    _authorise(session, user, entity, Action.DELETE, request)
    try:
        result = service.void_transaction(
            session, user, entity, record_id, reason=reason,
            ip_address=audit.client_ip(request),
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _handle(exc, f"{data_type} void") from exc
    return result.to_dict()


@router.post("/api/transactions/{data_type}/{record_id}/restore")
def restore_transaction_record(
    data_type: str,
    record_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    entity = _transaction_or_404(data_type)
    _authorise(session, user, entity, Action.DELETE, request)
    try:
        result = service.unvoid_transaction(
            session, user, entity, record_id,
            ip_address=audit.client_ip(request),
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _handle(exc, f"{data_type} restore") from exc
    return result.to_dict()


@router.get("/api/transactions/{data_type}/export/{fmt}")
def export_transactions(
    data_type: str,
    fmt: str,
    request: Request,
    search: str | None = Query(None, max_length=200),
    sort_by: str | None = Query(None, max_length=64),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    include_voided: bool = Query(False),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    codes: str | None = Query(None),
    columns: str | None = Query(None),
    business: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> Response:
    entity = _transaction_or_404(data_type)
    _authorise(session, user, entity, Action.EXPORT, request)
    filters = _entity_filters(entity, dict(request.query_params))
    return _export(session, user, entity, fmt, request, _list_request(
        search=search, sort_by=sort_by, sort_dir=sort_dir, page=1,
        page_size=export.CHUNK, include_deleted=False,
        include_voided=include_voided, filters=filters, codes=codes,
        date_from=date_from, date_to=date_to, business=business,
    ), columns)


def _export(session: Session, user: UserContext, entity: ManagedEntity,
            fmt: str, request: Request, list_request: ListRequest,
            columns: str | None) -> Response:
    """Shared export path for both halves of the module."""
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Unknown export format '{fmt}'. Use csv or xlsx.")
    wanted = [c for c in (columns or "").split(",") if c.strip()] or None
    try:
        chosen, rows = export.collect(session, user, entity, list_request,
                                      wanted)
        body = (export.to_csv(entity, chosen, rows) if fmt == "csv"
                else export.to_xlsx(entity, chosen, rows))
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc, f"{entity.key} export") from exc

    audit.record(session, action=AuditAction.EXPORT, user_id=user.user_id,
                 username=user.username, resource=f"{entity.key}:export",
                 ip_address=audit.client_ip(request),
                 detail={"format": fmt, "rows": len(rows)})
    session.commit()

    name = export.filename(entity, fmt)
    return Response(
        content=body,
        media_type=export.CSV_MEDIA_TYPE if fmt == "csv"
        else export.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
