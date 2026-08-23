"""WhatsApp integration layer.

The flow is deliberately thin, because the intelligence already exists:

    WhatsApp -> webhook -> identify user -> permission check
             -> Phase 3 agent -> tool -> result -> WhatsApp reply

No provider is faked. :class:`WhatsAppProvider` is an abstraction with one
real-shaped implementation for the WhatsApp Business Cloud API and one
:class:`NullWhatsAppProvider` used when no credentials are configured — the
latter records what it *would* have sent instead of pretending to send it, so a
test or a dry run never gives a false impression of delivery.

Authorisation is by mapped phone number: an unknown number gets a refusal and
never a business figure. A phone number is a weak identifier, so
``WHATSAPP_REQUIRE_VERIFIED_PROFILE`` can additionally require the provider's
verified profile name to match the account on file.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("app.integrations.whatsapp")

MAX_MESSAGE_LENGTH = 3500          # WhatsApp text messages cap around 4096
MAX_TABLE_ROWS = 10


@dataclass
class InboundMessage:
    """One normalised inbound WhatsApp message."""

    message_id: str
    from_number: str
    text: str
    profile_name: str | None = None
    timestamp: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundMessage:
    to_number: str
    text: str
    reply_to: str | None = None


@dataclass
class DeliveryResult:
    delivered: bool
    provider: str
    detail: str | None = None
    provider_message_id: str | None = None


class WhatsAppProvider(ABC):
    """What any WhatsApp transport must offer."""

    name = "abstract"

    @property
    @abstractmethod
    def configured(self) -> bool:
        """True when the provider has everything it needs to send."""

    @abstractmethod
    def send_text(self, message: OutboundMessage) -> DeliveryResult:
        """Deliver a plain-text message."""

    def verify_webhook(self, mode: str | None, token: str | None,
                       challenge: str | None) -> str | None:
        """Answer the provider's subscription handshake, or ``None`` to refuse."""
        expected = os.getenv("WHATSAPP_VERIFY_TOKEN")
        if not expected:
            logger.warning("WHATSAPP_VERIFY_TOKEN is not set; refusing verification")
            return None
        if mode == "subscribe" and token and hmac.compare_digest(token, expected):
            return challenge or ""
        logger.warning("rejected a webhook verification with a bad token")
        return None

    def verify_signature(self, payload: bytes, signature: str | None) -> bool:
        """Check the provider's payload signature when an app secret is set."""
        secret = os.getenv("WHATSAPP_APP_SECRET")
        if not secret:
            # No secret configured: signature checking is off. Deployments that
            # expose the webhook publicly must set one.
            return True
        if not signature:
            return False
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


class CloudApiWhatsAppProvider(WhatsAppProvider):
    """WhatsApp Business Cloud API transport.

    Enabled by setting ``WHATSAPP_API_TOKEN`` and ``WHATSAPP_PHONE_NUMBER_ID``.
    The token is read from the environment on every call and is never logged,
    stored or returned.
    """

    name = "whatsapp_cloud_api"
    api_version = os.getenv("WHATSAPP_API_VERSION", "v21.0")

    @property
    def configured(self) -> bool:
        return bool(os.getenv("WHATSAPP_API_TOKEN")
                    and os.getenv("WHATSAPP_PHONE_NUMBER_ID"))

    def send_text(self, message: OutboundMessage) -> DeliveryResult:
        if not self.configured:
            return DeliveryResult(False, self.name,
                                  "WhatsApp credentials are not configured.")
        import httpx

        phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        url = (f"https://graph.facebook.com/{self.api_version}/"
               f"{phone_number_id}/messages")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.to_number,
            "type": "text",
            "text": {"preview_url": False, "body": message.text[:MAX_MESSAGE_LENGTH]},
        }
        try:
            response = httpx.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {os.getenv('WHATSAPP_API_TOKEN')}"},
                timeout=float(os.getenv("WHATSAPP_TIMEOUT_SECONDS", "15")),
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - transport failures are reported
            logger.warning("WhatsApp send failed: %s", type(exc).__name__)
            return DeliveryResult(False, self.name, "The message could not be sent.")

        message_id = None
        try:
            message_id = body["messages"][0]["id"]
        except (KeyError, IndexError, TypeError):
            pass
        return DeliveryResult(True, self.name, provider_message_id=message_id)


class NullWhatsAppProvider(WhatsAppProvider):
    """Used when no credentials are configured.

    It records outbound messages rather than sending them, and says so. Nothing
    here pretends a message was delivered.
    """

    name = "not_configured"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    @property
    def configured(self) -> bool:
        return False

    def send_text(self, message: OutboundMessage) -> DeliveryResult:
        self.sent.append(message)
        logger.info("WhatsApp provider is not configured; message for %s was not sent",
                    _mask(message.to_number))
        return DeliveryResult(
            False, self.name,
            "No WhatsApp provider is configured, so the reply was not delivered.",
        )


def build_provider() -> WhatsAppProvider:
    """The configured provider, or the null provider."""
    provider = CloudApiWhatsAppProvider()
    return provider if provider.configured else NullWhatsAppProvider()


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def parse_webhook(payload: dict[str, Any]) -> list[InboundMessage]:
    """Extract text messages from a Cloud API webhook payload.

    Non-text messages (images, reactions, delivery statuses) are ignored rather
    than guessed at.
    """
    messages: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            profiles = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name")
                for contact in value.get("contacts") or []
            }
            for message in value.get("messages") or []:
                if message.get("type") != "text":
                    continue
                sender = message.get("from")
                body = ((message.get("text") or {}).get("body") or "").strip()
                if not sender or not body:
                    continue
                messages.append(InboundMessage(
                    message_id=message.get("id") or "",
                    from_number=normalize_number(sender),
                    text=body,
                    profile_name=profiles.get(sender),
                    timestamp=message.get("timestamp"),
                    raw=message,
                ))
    return messages


_NON_DIGITS = re.compile(r"[^\d]")


def normalize_number(number: str) -> str:
    """Digits only, so ``+880 17…`` and ``88017…`` compare equal."""
    return _NON_DIGITS.sub("", number or "")


def _mask(number: str) -> str:
    """Log-safe phone number: only the last four digits survive."""
    digits = normalize_number(number)
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


# ---------------------------------------------------------------------------
# Reply formatting
# ---------------------------------------------------------------------------

UNAUTHORIZED_REPLY = (
    "Your account is not authorized. Please ask your administrator to link this "
    "WhatsApp number to your user account."
)

LARGE_REPORT_REPLY = (
    "Report generated. Download/export functionality will be handled by the "
    "configured integration."
)


def format_reply(answer: str, rows: list[dict[str, Any]] | None = None) -> str:
    """Turn an agent answer into WhatsApp-friendly text.

    Markdown tables do not render in WhatsApp, so a ranking becomes a numbered
    list and anything too long is summarised rather than truncated mid-number.
    """
    text = _strip_markdown(answer)
    if rows and _looks_like_ranking(rows):
        listed = _render_ranking(rows)
        if listed:
            text = _drop_table(text) + "\n\n" + listed

    if len(text) > MAX_MESSAGE_LENGTH:
        head = text[:MAX_MESSAGE_LENGTH - len(LARGE_REPORT_REPLY) - 4].rstrip()
        return f"{head}\n\n{LARGE_REPORT_REPLY}"
    return text


def _strip_markdown(text: str) -> str:
    """WhatsApp uses *bold*, not **bold**, and renders no headings or tables."""
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^_(.+)_$", r"\1", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def _drop_table(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith("|")
    ).strip()


def _looks_like_ranking(rows: list[dict[str, Any]]) -> bool:
    if not rows or not isinstance(rows[0], dict):
        return False
    return "label" in rows[0] or "code" in rows[0]


#: Measures worth quoting in a one-line ranking, in priority order.
_RANK_MEASURES = ("net_sales", "target_amount", "total_stock",
                  "unrestricted_stock", "achievement_percent")


def _render_ranking(rows: list[dict[str, Any]]) -> str:
    from ..ai.response_formatter import (COLUMN_FORMATS, format_amount,
                                         format_percent, format_stock)

    measure = next((m for m in _RANK_MEASURES if m in rows[0]), None)
    if measure is None:
        return ""
    # Stock is neither money nor a ratio: sending it through ``format_amount``
    # put a taka sign on a plant's position. Its unit is named once, in a
    # heading above the list taken from the same column catalogue the web tables
    # use, and never repeated after each number — the convention every other
    # stock surface follows.
    is_stock = measure.endswith("_stock")
    lines = []
    if is_stock:
        lines.append(f"*{COLUMN_FORMATS[measure][0]}*")
    for position, row in enumerate(rows[:MAX_TABLE_ROWS], start=1):
        name = row.get("label") or row.get("code") or "—"
        value = row.get(measure)
        if measure.endswith("_percent"):
            rendered = format_percent(value)
        elif is_stock:
            rendered = format_stock(value)
        else:
            rendered = format_amount(value)
        lines.append(f"{position}. {name} — {rendered}")
    if len(rows) > MAX_TABLE_ROWS:
        lines.append(f"…and {len(rows) - MAX_TABLE_ROWS} more.")
    return "\n".join(lines)


__all__ = [
    "WhatsAppProvider",
    "CloudApiWhatsAppProvider",
    "NullWhatsAppProvider",
    "build_provider",
    "InboundMessage",
    "OutboundMessage",
    "DeliveryResult",
    "parse_webhook",
    "normalize_number",
    "format_reply",
    "UNAUTHORIZED_REPLY",
    "LARGE_REPORT_REPLY",
    "MAX_MESSAGE_LENGTH",
]
