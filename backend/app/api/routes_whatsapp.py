"""WhatsApp webhook endpoints.

``GET`` answers the provider's subscription handshake; ``POST`` receives
messages. Both are unauthenticated by necessity — the provider calls them — so
they are protected instead by the verify token and, when an app secret is
configured, an HMAC signature check on the raw body.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models_ai import AppUser
from ..integrations.whatsapp import InboundMessage, build_provider, parse_webhook
from ..integrations.whatsapp_service import WhatsAppService
from .deps import get_current_user, get_session, internal_error, require_admin

logger = logging.getLogger("app.api.whatsapp")

router = APIRouter(prefix="/api/integrations/whatsapp", tags=["integrations"])


@router.get("/webhook")
def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Subscription handshake.

    Returns the challenge only when the token matches ``WHATSAPP_VERIFY_TOKEN``;
    anything else is a 403 with no detail about why.
    """
    provider = build_provider()
    challenge = provider.verify_webhook(hub_mode, hub_verify_token, hub_challenge)
    if challenge is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Verification failed.")
    return Response(content=challenge, media_type="text/plain")


@router.post("/webhook")
async def receive_webhook(request: Request,
                          session: Session = Depends(get_session)) -> dict[str, Any]:
    """Receive inbound messages and reply through the agent.

    Always answers 200 once the signature passes: providers retry on any other
    status, and a retry storm caused by one malformed message helps nobody. The
    per-message outcome is reported in the body and written to the audit log.
    """
    body = await request.body()
    provider = build_provider()

    if not provider.verify_signature(body, request.headers.get("x-hub-signature-256")):
        logger.warning("rejected a WhatsApp webhook with an invalid signature")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid signature.")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is not retried
        logger.warning("rejected a WhatsApp webhook with an unreadable body")
        return {"status": "ignored", "reason": "unreadable payload", "handled": 0}

    messages = parse_webhook(payload if isinstance(payload, dict) else {})
    if not messages:
        return {"status": "ignored", "reason": "no text messages", "handled": 0}

    service = WhatsAppService(session, provider=provider)
    results = []
    for message in messages:
        try:
            handled = service.handle(message)
        except Exception:  # noqa: BLE001 - one bad message must not fail the batch
            logger.exception("failed to handle a WhatsApp message")
            session.rollback()
            results.append({"authorized": False, "delivered": False,
                            "error": "processing failed"})
            continue
        results.append({
            "authorized": handled.authorized,
            "intent": handled.intent,
            "delivered": bool(handled.delivery and handled.delivery.delivered),
            "delivery_detail": handled.delivery.detail if handled.delivery else None,
        })

    return {"status": "ok", "handled": len(results), "results": results}


@router.get("/status")
def integration_status(session: Session = Depends(get_session),
                       _: UserContext = Depends(require_admin)) -> dict[str, Any]:
    """Integration readiness for the admin panel. Never returns a token."""
    try:
        return WhatsAppService(session).status()
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "whatsapp status") from exc


@router.post("/simulate")
def simulate(
    message: dict[str, Any],
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Dry-run the WhatsApp flow as the signed-in user, without sending anything.

    Lets an administrator confirm the mapping and the reply format before a
    provider is connected. ``send=False`` guarantees no outbound call is made.
    """
    text = str(message.get("text") or "").strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "A message text is required.")

    record = session.get(AppUser, user.user_id)
    if record is None or not record.phone_number:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Your account has no WhatsApp number linked, so the flow cannot be "
            "simulated. Ask an administrator to add one.",
        )

    service = WhatsAppService(session)
    handled = service.handle(
        InboundMessage(message_id="simulated", from_number=record.phone_number,
                       text=text, profile_name=record.display_name),
        send=False,
    )
    return {
        "authorized": handled.authorized,
        "intent": handled.intent,
        "reply": handled.reply,
        "delivered": False,
        "note": "Simulation only — no message was sent to WhatsApp.",
    }
