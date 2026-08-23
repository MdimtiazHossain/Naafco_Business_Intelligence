"""LLM client abstraction.

Two implementations satisfy one interface:

* :class:`OpenAIClient` — production. Uses OpenAI tool calling with the tool
  definitions generated from the Pydantic input schemas, and structured outputs
  for planning.
* :class:`NullLLMClient` — used automatically when ``OPENAI_API_KEY`` is not
  configured. It reports that no model is available; the orchestrator then falls
  back to the deterministic planner and the deterministic formatter.

That fallback is what lets the agent be developed, tested and demonstrated
without a key — and it means an API outage degrades the agent to rule-based
answers rather than taking it down. In both paths the LLM only ever *chooses*
a tool and *phrases* prose. It never touches the database, never sees
credentials, and cannot alter permission filters.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

logger = logging.getLogger("app.ai.llm")

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
DEFAULT_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))


@dataclass
class ToolCallRequest:
    """A tool the model asked to run."""

    name: str
    arguments: dict[str, Any]
    call_id: str | None = None


@dataclass
class LLMResponse:
    """What the model returned: prose, tool calls, or neither."""

    text: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    model: str | None = None
    available: bool = True
    error: str | None = None

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    """The contract the orchestrator depends on."""

    @property
    def available(self) -> bool:
        ...

    def plan(self, messages: Sequence[dict[str, str]],
             tools: Sequence[dict[str, Any]]) -> LLMResponse:
        """Pick a tool (or reply directly)."""

    def write_answer(self, messages: Sequence[dict[str, str]]) -> LLMResponse:
        """Phrase an answer from already-validated tool output."""


class NullLLMClient:
    """Stand-in used when no API key is configured."""

    reason = (
        "OPENAI_API_KEY is not configured, so the deterministic planner and "
        "formatter are being used."
    )

    @property
    def available(self) -> bool:
        return False

    def plan(self, messages: Sequence[dict[str, str]],
             tools: Sequence[dict[str, Any]]) -> LLMResponse:
        return LLMResponse(available=False, error=self.reason)

    def write_answer(self, messages: Sequence[dict[str, str]]) -> LLMResponse:
        return LLMResponse(available=False, error=self.reason)


class OpenAIClient:
    """OpenAI tool-calling client.

    The key is read from the environment only. It is never logged, never stored
    and never returned in any response.
    """

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.model = model
        self.timeout = timeout
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client: Any = None
        if self._api_key:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=self._api_key, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - never fatal, we degrade
                logger.warning("OpenAI client could not be created: %s", type(exc).__name__)
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def plan(self, messages: Sequence[dict[str, str]],
             tools: Sequence[dict[str, Any]]) -> LLMResponse:
        """Ask the model to choose one tool and its arguments."""
        if not self.available:
            return LLMResponse(available=False, error="OpenAI client is not configured")
        try:
            completion = self._client.chat.completions.create(
                model=self.model,
                messages=list(messages),
                tools=list(tools),
                tool_choice="auto",
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001 - degraded, never surfaced raw
            logger.warning("OpenAI planning call failed: %s", type(exc).__name__)
            return LLMResponse(available=False, error="the model could not be reached")

        choice = completion.choices[0].message
        calls: list[ToolCallRequest] = []
        for call in getattr(choice, "tool_calls", None) or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                logger.warning("model returned unparseable arguments for %s",
                               call.function.name)
                continue
            if not isinstance(arguments, dict):
                continue
            calls.append(ToolCallRequest(
                name=call.function.name, arguments=arguments, call_id=call.id
            ))
        return LLMResponse(text=choice.content, tool_calls=calls, model=self.model)

    def write_answer(self, messages: Sequence[dict[str, str]]) -> LLMResponse:
        """Phrase the final answer. Figures are supplied pre-formatted."""
        if not self.available:
            return LLMResponse(available=False, error="OpenAI client is not configured")
        try:
            completion = self._client.chat.completions.create(
                model=self.model,
                messages=list(messages),
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenAI answer call failed: %s", type(exc).__name__)
            return LLMResponse(available=False, error="the model could not be reached")
        return LLMResponse(text=completion.choices[0].message.content, model=self.model)


def build_llm_client(api_key: str | None = None) -> LLMClient:
    """The configured client, or the null client when no key is present."""
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        return NullLLMClient()
    client = OpenAIClient(api_key=key)
    return client if client.available else NullLLMClient()


__all__ = [
    "LLMClient",
    "LLMResponse",
    "ToolCallRequest",
    "OpenAIClient",
    "NullLLMClient",
    "build_llm_client",
    "DEFAULT_MODEL",
]
