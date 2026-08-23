"""Phase 3: the AI Business Intelligence agent.

    chat API -> orchestrator -> intent / entity / date resolution
             -> permission check -> tool selection -> tool execution
             -> validation -> formatting -> answer

The LLM never touches the database. It selects an approved tool and phrases
prose; every figure comes from a validated tool result.
"""

from .agent import BusinessIntelligenceAgent
from .exceptions import (
    AgentError,
    AmbiguousEntityError,
    EntityNotFoundError,
    NoDataError,
    PermissionDeniedError,
)
from .llm import build_llm_client
from .orchestrator import AgentAnswer, ConversationContext, Orchestrator
from .permission_filter import PermissionFilter, UserContext, load_user_context
from .schemas import ChatRequest, ChatResponse, Intent, StructuredQuery
from .tools import REGISTRY as TOOL_REGISTRY

__all__ = [
    "BusinessIntelligenceAgent",
    "Orchestrator",
    "AgentAnswer",
    "ConversationContext",
    "UserContext",
    "PermissionFilter",
    "load_user_context",
    "build_llm_client",
    "Intent",
    "StructuredQuery",
    "ChatRequest",
    "ChatResponse",
    "TOOL_REGISTRY",
    "AgentError",
    "PermissionDeniedError",
    "AmbiguousEntityError",
    "EntityNotFoundError",
    "NoDataError",
]
