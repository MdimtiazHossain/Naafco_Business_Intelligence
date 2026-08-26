"""Agent learning review API: the queue, and the approvals taken from it.

Guarded by ``SectionKey.AGENT_LEARNING``, which is off by default for everyone
and grantable on its own — reviewing what a word means is a business judgement,
and the person who holds it need not be a system administrator.

Every state change here is audited. An approved alias changes how the assistant
reads a question for everybody, so "who decided this, and when" has to be
answerable later without reading the row's own columns.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai import lexicon, vocabulary
from ..ai.exceptions import AgentError
from ..ai.tools import REGISTRY
from ..ai.permission_filter import UserContext
from ..auth import audit
from ..auth.permissions import require_action, require_section
from ..database.models_ai import AuditAction
from ..database.models_learning import (
    AliasKind,
    LearningSource,
    LearningStatus,
    SignalStatus,
    SignalType,
)
from ..security.sections import Action, SectionKey
from .deps import get_session, internal_error

logger = logging.getLogger("app.api.learning")

router = APIRouter(prefix="/api/learning", tags=["agent-learning"])

_VIEW = require_section(SectionKey.AGENT_LEARNING)
#: Proposing writes a row; approving changes what the assistant reads for
#: everybody. Both are gated on their own action rather than on VIEW, so a
#: reviewer can be given read-only sight of the queue.
_CREATE = require_action(SectionKey.AGENT_LEARNING, Action.CREATE)
_EDIT = require_action(SectionKey.AGENT_LEARNING, Action.EDIT)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SignalStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["NEW", "TRIAGED", "PROPOSED", "DISMISSED"]


class AliasProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(min_length=1, max_length=200)
    alias_kind: Literal["ENTITY", "METRIC", "GROUP_BY", "MODIFIER"]
    entity_type: str | None = Field(default=None, max_length=32)
    entity_code: str | None = Field(default=None, max_length=64)
    target_keyword: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=8)
    signal_id: int | None = None
    notes: str | None = Field(default=None, max_length=1000)


class ExampleProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    tool_name: str = Field(min_length=1, max_length=64)
    intent: str | None = Field(default=None, max_length=48)
    arguments: dict[str, Any] | None = None
    language: str | None = Field(default=None, max_length=8)
    source_message_id: int | None = None
    notes: str | None = Field(default=None, max_length=1000)


class DecisionRequest(BaseModel):
    """Approve, reject or retire. ``replace`` only means anything on approve."""

    model_config = ConfigDict(extra="forbid")

    replace: bool = False
    reason: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _commit(session: Session, user: UserContext, http_request: Request, *,
            resource: str, audit_action: str, detail: dict[str, Any],
            work) -> Any:
    """Run one review action, audit it and commit — or surface a clean refusal."""
    try:
        result = work()
        audit.record(
            session, action=audit_action, user_id=user.user_id,
            username=user.username, resource=resource,
            ip_address=audit.client_ip(http_request), detail=detail,
        )
        session.commit()
        # After the commit, never before: bumping first would let a concurrent
        # question load the uncommitted state and cache it under the new
        # generation, leaving the committed vocabulary unreachable until
        # something else happened to invalidate again.
        lexicon.invalidate_cache()
        return result
    except AgentError as exc:
        session.rollback()
        # 409: the request was well-formed, the *state* refuses it — an alias
        # already active, a phrase already spoken for, a keyword that is not
        # real. The reason is the whole value to a reviewer, so it is shown.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": exc.code, "message": exc.user_message},
        ) from exc
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - internals never reach the caller
        session.rollback()
        raise internal_error(exc, "learning") from exc


def _read(work) -> Any:
    try:
        return work()
    except AgentError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": exc.code, "message": exc.user_message},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "learning") from exc


# ---------------------------------------------------------------------------
# Vocabulary the reviewer may choose from
# ---------------------------------------------------------------------------


@router.get("/options")
def options(user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """Everything the proposal form needs, derived rather than restated.

    A keyword added to ``ai.intent`` appears here with no frontend change, and
    one removed stops being offered — the same rule every other registry in this
    project follows.
    """
    return {
        "alias_kinds": list(AliasKind.ALL),
        "alias_targets": vocabulary.alias_targets(),
        "signal_types": list(SignalType.ALL),
        "signal_statuses": list(SignalStatus.ALL),
        "statuses": list(LearningStatus.ALL),
        "sources": list(LearningSource.ALL),
        # Read straight off the tool registry, so a tool added or removed on the
        # server changes what a reviewer may pin an example to with no edit
        # here and none in the browser.
        "tools": [
            {"name": name, "description": spec.description}
            for name, spec in sorted(REGISTRY.items())
        ],
    }


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


@router.get("/signals")
def list_signals(
    signal_status: str | None = Query(default=None),
    signal_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """Mined failures, most frequent first."""
    page = _read(lambda: vocabulary.list_signals(
        session, status=signal_status, signal_type=signal_type,
        limit=limit, offset=offset,
    ))
    return {"signals": page.rows, "total": page.total}


@router.patch("/signals/{signal_id}")
def set_signal_status(
    signal_id: int,
    request: SignalStatusRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    return _commit(
        session, user, http_request, resource="learning_signal",
        audit_action=AuditAction.AGENT_SIGNAL_TRIAGED,
        detail={"signal_id": signal_id, "status": request.status},
        work=lambda: vocabulary.set_signal_status(
            session, user, signal_id, request.status,
        ),
    )


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


@router.get("/aliases")
def list_aliases(
    alias_status: str | None = Query(default=None),
    alias_kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    page = _read(lambda: vocabulary.list_aliases(
        session, status=alias_status, alias_kind=alias_kind,
        limit=limit, offset=offset,
    ))
    return {"aliases": page.rows, "total": page.total}


@router.post("/aliases", status_code=status.HTTP_201_CREATED)
def propose_alias(
    request: AliasProposal,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_CREATE),
) -> dict[str, Any]:
    """Propose a phrase and what it means. Inert until approved."""
    return _commit(
        session, user, http_request, resource="learning_alias",
        audit_action=AuditAction.AGENT_LEARNING_PROPOSED,
        detail={"phrase": request.phrase, "kind": request.alias_kind},
        work=lambda: vocabulary.propose_alias(
            session, user, phrase=request.phrase, alias_kind=request.alias_kind,
            entity_type=request.entity_type, entity_code=request.entity_code,
            target_keyword=request.target_keyword, language=request.language,
            signal_id=request.signal_id, notes=request.notes,
            source=LearningSource.MANUAL,
        ),
    )


@router.post("/aliases/{alias_id}/approve")
def approve_alias(
    alias_id: int,
    request: DecisionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    """Make an alias live. From here the resolver may read it."""
    return _commit(
        session, user, http_request, resource="learning_alias",
        audit_action=AuditAction.AGENT_LEARNING_APPROVED,
        detail={"alias_id": alias_id, "replace": request.replace},
        work=lambda: vocabulary.approve_alias(
            session, user, alias_id, replace=request.replace,
        ),
    )


@router.post("/aliases/{alias_id}/reject")
def reject_alias(
    alias_id: int,
    request: DecisionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    return _commit(
        session, user, http_request, resource="learning_alias",
        audit_action=AuditAction.AGENT_LEARNING_REJECTED,
        detail={"alias_id": alias_id},
        work=lambda: vocabulary.reject_alias(
            session, user, alias_id, reason=request.reason,
        ),
    )


@router.post("/aliases/{alias_id}/retire")
def retire_alias(
    alias_id: int,
    request: DecisionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    """Stop the resolver reading an alias. The row stays, and stays readable."""
    return _commit(
        session, user, http_request, resource="learning_alias",
        audit_action=AuditAction.AGENT_LEARNING_RETIRED,
        detail={"alias_id": alias_id},
        work=lambda: vocabulary.retire_alias(
            session, user, alias_id, reason=request.reason,
        ),
    )


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


@router.get("/examples")
def list_examples(
    example_status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    page = _read(lambda: vocabulary.list_examples(
        session, status=example_status, limit=limit, offset=offset,
    ))
    return {"examples": page.rows, "total": page.total}


@router.post("/examples", status_code=status.HTTP_201_CREATED)
def propose_example(
    request: ExampleProposal,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_CREATE),
) -> dict[str, Any]:
    return _commit(
        session, user, http_request, resource="learning_example",
        audit_action=AuditAction.AGENT_LEARNING_PROPOSED,
        detail={"tool": request.tool_name},
        work=lambda: vocabulary.propose_example(
            session, user, question=request.question,
            tool_name=request.tool_name, intent=request.intent,
            arguments=request.arguments, language=request.language,
            source_message_id=request.source_message_id, notes=request.notes,
            source=LearningSource.MANUAL,
        ),
    )


@router.post("/examples/{example_id}/approve")
def approve_example(
    example_id: int,
    request: DecisionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    return _commit(
        session, user, http_request, resource="learning_example",
        audit_action=AuditAction.AGENT_LEARNING_APPROVED,
        detail={"example_id": example_id, "replace": request.replace},
        work=lambda: vocabulary.approve_example(
            session, user, example_id, replace=request.replace,
        ),
    )


@router.post("/examples/{example_id}/reject")
def reject_example(
    example_id: int,
    request: DecisionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    return _commit(
        session, user, http_request, resource="learning_example",
        audit_action=AuditAction.AGENT_LEARNING_REJECTED,
        detail={"example_id": example_id},
        work=lambda: vocabulary.reject_example(
            session, user, example_id, reason=request.reason,
        ),
    )


@router.post("/examples/{example_id}/retire")
def retire_example(
    example_id: int,
    request: DecisionRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = Depends(_EDIT),
) -> dict[str, Any]:
    return _commit(
        session, user, http_request, resource="learning_example",
        audit_action=AuditAction.AGENT_LEARNING_RETIRED,
        detail={"example_id": example_id},
        work=lambda: vocabulary.retire_example(
            session, user, example_id, reason=request.reason,
        ),
    )
