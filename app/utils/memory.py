"""
Conversation memory and intent tracking.

Lightweight memory system that tracks conversation history
and detects user intent (new/followup/refinement).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class Message:
    """A single message in conversation history."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: float = field(default_factory=time.monotonic)


class ConversationMemory:
    """Manages conversation history per session."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.messages: list[Message] = []
        self._last_intent: str = "new"

    def add_message(self, role: str, content: str) -> None:
        """Add a message to conversation history."""
        self.messages.append(Message(role=role, content=content))

    def add_user_message(self, content: str) -> None:
        self.add_message("user", content)

    def add_assistant_message(self, content: str) -> None:
        self.add_message("assistant", content)

    def get_context(self) -> str:
        """Build conversation context from history.

        Returns formatted context string with truncation.
        """
        if not settings.memory.enabled:
            return ""

        if not self.messages:
            return ""

        max_history = settings.memory.max_history_messages
        max_chars = settings.memory.max_context_chars

        # Take last N messages
        history = (
            self.messages[-max_history:] if len(self.messages) > max_history else self.messages
        )

        # Build context
        lines = ["Conversation context:"]
        for msg in history:
            role_label = "User" if msg.role == "user" else "Assistant"
            content = msg.content[:500]  # Truncate each message individually
            lines.append(f"{role_label}: {content}")

        # Join and truncate to max context chars
        context = "\n".join(lines)
        if len(context) > max_chars:
            context = context[:max_chars] + "..."

        logger.info(
            "memory_context_built",
            session_id=self.session_id,
            message_count=len(history),
            context_length=len(context),
        )

        return context

    def reset(self) -> None:
        """Reset conversation history (for new intent)."""
        self.messages.clear()
        self._last_intent = "new"

    @property
    def last_intent(self) -> str:
        return self._last_intent

    @last_intent.setter
    def last_intent(self, value: str) -> None:
        self._last_intent = value


def detect_intent(query: str) -> str:
    """Detect user intent from query.

    Types:
    - new: Fresh question, no relation to previous
    - followup: Uses pronouns (он, это, etc.)
    - refinement: Asks for more detail (подробнее, объясни)

    Returns: "new" | "followup" | "refinement"
    """
    if not settings.memory.enable_intent_tracking:
        return "new"

    query_lower = query.lower()

    # Refinement keywords
    refinement_keywords = [
        "подробнее",
        "объясни",
        "расскажи подробнее",
        "больше деталей",
        "уточни",
        "дополни",
        "more details",
        "explain",
        "elaborate",
    ]
    for keyword in refinement_keywords:
        if keyword in query_lower:
            return "refinement"

    # Followup pronouns
    followup_pronouns = [
        "он",
        "она",
        "оно",
        "они",
        "это",
        "этот",
        "такой",
        "those",
        "this",
        "it",
        "them",
        "his",
        "her",
        "its",
    ]
    for pronoun in followup_pronouns:
        if pronoun in query_lower:
            return "followup"

    return "new"


def get_intent_based_depth(intent: str) -> int:
    """Get appropriate memory depth based on intent.

    - followup → increase depth (keep more context)
    - refinement → keep full memory
    - new → reset memory
    """
    if intent == "followup":
        return min(settings.memory.max_history_messages + 2, 20)
    elif intent == "refinement":
        return settings.memory.max_history_messages
    else:
        return 0  # Reset for new intent


# In-memory storage per session
_memory_store: dict[str, ConversationMemory] = {}


def get_memory(session_id: str) -> ConversationMemory:
    """Get or create memory for session."""
    if session_id not in _memory_store:
        _memory_store[session_id] = ConversationMemory(session_id)
    return _memory_store[session_id]


def clear_memory(session_id: str) -> None:
    """Clear memory for session."""
    _memory_store.pop(session_id, None)
