"""WhatsApp conversation service.

Maps an inbound number to a user, then hands the message to the **same** Phase 3
agent the web UI uses. There is no separate WhatsApp reporting path, so a
question answered in chat and the same question over WhatsApp return the same
numbers under the same permissions.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.agent import BusinessIntelligenceAgent
from ..ai.llm import build_llm_client
from ..ai.permission_filter import UserContext
from ..auth import audit
from ..database.models_ai import AppUser, AuditAction
from .whatsapp import (
    UNAUTHORIZED_REPLY,
    DeliveryResult,
    InboundMessage,
    OutboundMessage,
    WhatsAppProvider,
    build_provider,
    format_reply,
    normalize_number,
)

logger = logging.getLogger("app.integrations.whatsapp_service")


def require_verified_profile() -> bool:
    """Whether the provider's verified profile name must also match.

    A phone number alone is a weak identifier — numbers get recycled and caller
    ID can be spoofed on some transports. Where the provider supplies a verified
    profile name, requiring it adds a second factor.
    """
    return os.getenv("WHATSAPP_REQUIRE_VERIFIED_PROFILE", "false").lower() in (
        "1", "true", "yes"
    )


@dataclass
class HandledMessage:
    """What the service did with one inbound message."""

    from_number: str
    authorized: bool
    reply: str
    username: str | None = None
    intent: str | None = None
    conversation_id: str | None = None
    delivery: DeliveryResult | None = None


class WhatsAppService:
    """Identify, authorise, answer, reply."""

    def __init__(self, session: Session, provider: WhatsAppProvider | None = None,
                 agent_factory=None) -> None:
        self.session = session
        self.provider = provider or build_provider()
        self._agent_factory = agent_factory or self._default_agent

    @staticmethod
    def _default_agent(session: Session, user: UserContext):
        return BusinessIntelligenceAgent(session, user, llm=build_llm_client())

    # -- identity -----------------------------------------------------------

    def resolve_user(self, message: InboundMessage) -> AppUser | None:
        """Find the active account linked to this number.

        Comparison is on digits only, so stored ``+8801…`` matches inbound
        ``8801…``. An unknown or inactive number resolves to nothing.
        """
        digits = normalize_number(message.from_number)
        if not digits:
            return None

        candidates = self.session.execute(
            select(AppUser).where(AppUser.phone_number.isnot(None),
                                  AppUser.is_active.is_(True))
        ).scalars().all()
        matched = next(
            (user for user in candidates
             if normalize_number(user.phone_number or "") == digits),
            None,
        )
        if matched is None:
            return None

        if require_verified_profile():
            expected = (matched.display_name or "").strip().casefold()
            actual = (message.profile_name or "").strip().casefold()
            if not actual or actual != expected:
                logger.warning(
                    "WhatsApp profile name did not match for user %s; refusing",
                    matched.username,
                )
                return None
        return matched

    # -- handling -----------------------------------------------------------

    def handle(self, message: InboundMessage, *, send: bool = True) -> HandledMessage:
        """Answer one inbound message."""
        user_row = self.resolve_user(message)

        if user_row is None:
            audit.record(
                self.session, action=AuditAction.WHATSAPP_MESSAGE,
                resource="whatsapp", success=False,
                detail={"reason": "unknown_number", "number_suffix":
                        normalize_number(message.from_number)[-4:]},
            )
            self.session.commit()
            handled = HandledMessage(
                from_number=message.from_number, authorized=False,
                reply=UNAUTHORIZED_REPLY,
            )
            if send:
                handled.delivery = self._send(handled)
            return handled

        user = UserContext.from_user(user_row)
        agent = self._agent_factory(self.session, user)
        response = agent.chat(message.text)

        rows = (response.data or {}).get("rows") or []
        reply = format_reply(response.answer, rows)

        audit.record(
            self.session, action=AuditAction.WHATSAPP_MESSAGE,
            user_id=user.user_id, username=user.username, resource="whatsapp",
            detail={"intent": response.intent.value,
                    "conversation_id": response.conversation_id,
                    "tools": response.tools_used},
            success=response.error_code is None,
        )
        self.session.commit()

        handled = HandledMessage(
            from_number=message.from_number, authorized=True, reply=reply,
            username=user.username, intent=response.intent.value,
            conversation_id=response.conversation_id,
        )
        if send:
            handled.delivery = self._send(handled)
        return handled

    def _send(self, handled: HandledMessage) -> DeliveryResult:
        return self.provider.send_text(
            OutboundMessage(to_number=handled.from_number, text=handled.reply)
        )

    def status(self) -> dict[str, Any]:
        """Integration readiness, for the admin panel. Never returns a token."""
        linked = self.session.execute(
            select(AppUser).where(AppUser.phone_number.isnot(None),
                                  AppUser.is_active.is_(True))
        ).scalars().all()
        return {
            "provider": self.provider.name,
            "configured": self.provider.configured,
            "verify_token_set": bool(os.getenv("WHATSAPP_VERIFY_TOKEN")),
            "signature_check_enabled": bool(os.getenv("WHATSAPP_APP_SECRET")),
            "require_verified_profile": require_verified_profile(),
            "linked_users": len(linked),
        }


__all__ = ["WhatsAppService", "HandledMessage", "require_verified_profile"]
