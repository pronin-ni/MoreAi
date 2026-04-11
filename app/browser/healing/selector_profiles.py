"""
Selector profiles — declarative definitions of expected element shapes.

Each profile describes a UI role (message_input, send_button, etc.) with:
- primary selectors (tried first)
- fallback selectors (tried second)
- semantic hints (role, tag, aria attributes for healing)
- expected element properties (tag, editable, clickable, etc.)
- container context hints (optional parent/container selectors)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SelectorProfile:
    """Declarative description of a UI element role.

    Attributes
    ----------
    role : semantic role name (message_input, send_button, …)
    primary : ordered list of CSS/role selectors tried first
    fallback : ordered list of fallback selectors tried if primary fails
    expected_tag : expected HTML tag (textarea, button, input, …)
    expected_role : expected ARIA role (textbox, button, …)
    is_editable : element should be editable (for inputs)
    is_clickable : element should be clickable (for buttons)
    must_be_visible : element must be visible
    placeholder_hint : expected placeholder text (fuzzy match)
    aria_label_hint : expected aria-label (fuzzy match)
    container_selector : optional parent/container selector to narrow search
    min_confidence : minimum confidence threshold for healing (0.0-1.0)
    """

    role: str
    primary: tuple[str, ...] = ()
    fallback: tuple[str, ...] = ()
    expected_tag: str = ""
    expected_role: str = ""
    is_editable: bool = False
    is_clickable: bool = False
    must_be_visible: bool = True
    placeholder_hint: str = ""
    aria_label_hint: str = ""
    container_selector: str = ""
    min_confidence: float = 0.7

    # Healing-specific semantic hints
    # Keywords to look for in element text, aria-label, placeholder
    semantic_keywords: tuple[str, ...] = ()
    # Negative keywords that disqualify a candidate
    negative_keywords: tuple[str, ...] = ()

    # Attributes that a valid candidate should have
    expected_attributes: dict[str, str] = field(default_factory=dict)


# ── Predefined profiles for common roles ──

MESSAGE_INPUT = SelectorProfile(
    role="message_input",
    expected_tag="textarea",
    expected_role="textbox",
    is_editable=True,
    must_be_visible=True,
    min_confidence=0.7,
)

SEND_BUTTON = SelectorProfile(
    role="send_button",
    expected_tag="button",
    expected_role="button",
    is_clickable=True,
    must_be_visible=True,
    min_confidence=0.7,
)

ASSISTANT_MESSAGE = SelectorProfile(
    role="assistant_message",
    expected_tag="div",
    is_editable=False,
    must_be_visible=True,
    min_confidence=0.6,
)

NEW_CHAT_BUTTON = SelectorProfile(
    role="new_chat_button",
    expected_tag="button",
    expected_role="button",
    is_clickable=True,
    must_be_visible=True,
    min_confidence=0.7,
)

CHAT_READY_INDICATOR = SelectorProfile(
    role="chat_ready_indicator",
    expected_tag="textarea",
    expected_role="textbox",
    is_editable=True,
    must_be_visible=True,
    min_confidence=0.8,
)


# ── Provider-specific profile builders ──

def build_provider_profiles(provider_id: str) -> dict[str, SelectorProfile]:
    """Build provider-specific selector profiles.

    Returns a dict mapping role names to SelectorProfile instances.
    Providers that don't override this get generic profiles.
    """
    builders = {
        "qwen": _qwen_profiles,
        "chatgpt": _chatgpt_profiles,
        "glm": _glm_profiles,
        "yandex": _yandex_profiles,
        "kimi": _kimi_profiles,
        "deepseek": _deepseek_profiles,
    }
    builder = builders.get(provider_id)
    if builder:
        return builder()
    return _generic_profiles()


def _generic_profiles() -> dict[str, SelectorProfile]:
    return {
        "message_input": MESSAGE_INPUT,
        "send_button": SEND_BUTTON,
        "assistant_message": ASSISTANT_MESSAGE,
        "new_chat_button": NEW_CHAT_BUTTON,
    }


def _qwen_profiles() -> dict[str, SelectorProfile]:
    return {
        "message_input": SelectorProfile(
            role="message_input",
            primary=(
                'role=textbox[name="Чем я могу помочь"]',
                'textarea[placeholder*="Чем"]',
            ),
            fallback=(
                "textarea",
                "main textarea",
                '[role="textbox"]',
            ),
            expected_tag="textarea",
            expected_role="textbox",
            is_editable=True,
            must_be_visible=True,
            placeholder_hint="Чем",
            semantic_keywords=("чем", "помочь", "чт", "спрос"),
            container_selector="main",
            min_confidence=0.7,
        ),
        "send_button": SelectorProfile(
            role="send_button",
            primary=(
                'button:has(img[src*="send"])',
                "main button:nth(1)",
            ),
            fallback=(
                "button:has(img)",
                '[role="button"]',
                "main button",
            ),
            expected_tag="button",
            expected_role="button",
            is_clickable=True,
            must_be_visible=True,
            container_selector="main",
            semantic_keywords=("send", "отправ", "submit"),
            min_confidence=0.7,
        ),
        "assistant_message": SelectorProfile(
            role="assistant_message",
            primary=(
                "main p:last-of-type",
                "main > div > p",
            ),
            fallback=(
                '[class*="message"]',
                "main p",
                "main div",
            ),
            expected_tag="div",
            expected_role="",
            is_editable=False,
            must_be_visible=True,
            container_selector="main",
            semantic_keywords=("assistant", "response", "ответ"),
            min_confidence=0.6,
        ),
        "new_chat_button": SelectorProfile(
            role="new_chat_button",
            primary=(
                'role=button[name="Новый чат"]',
                "a[href*='new']",
            ),
            fallback=(
                '[class*="new-chat"]',
                "button",
                'a[href="/"]',
            ),
            expected_tag="button",
            expected_role="button",
            is_clickable=True,
            must_be_visible=True,
            semantic_keywords=("new", "новый", "chat"),
            min_confidence=0.7,
        ),
    }


def _chatgpt_profiles() -> dict[str, SelectorProfile]:
    return {
        "message_input": SelectorProfile(
            role="message_input",
            primary=(
                'role=textbox[name="Chat with ChatGPT"]',
                'role=textbox',
                'textarea[placeholder*="Ask"]',
            ),
            fallback=(
                "textarea",
                '[contenteditable="true"]',
            ),
            expected_tag="textarea",
            expected_role="textbox",
            is_editable=True,
            must_be_visible=True,
            placeholder_hint="Ask",
            semantic_keywords=("ask", "chat", "message", "prompt"),
            container_selector="main",
            min_confidence=0.7,
        ),
        "send_button": SelectorProfile(
            role="send_button",
            primary=(
                'button[data-testid="send-button"]',
                'button:has(svg[class*="send"])',
            ),
            fallback=(
                "main form button",
                "main button",
                '[aria-label*="send"]',
            ),
            expected_tag="button",
            expected_role="button",
            is_clickable=True,
            must_be_visible=True,
            container_selector="main",
            semantic_keywords=("send", "submit"),
            min_confidence=0.7,
        ),
        "assistant_message": SelectorProfile(
            role="assistant_message",
            primary=(
                '[data-message-author-role="assistant"]',
                "main div[tabindex='-1']",
            ),
            fallback=(
                "main div",
                '[class*="message"]',
            ),
            expected_tag="div",
            expected_role="",
            is_editable=False,
            must_be_visible=True,
            container_selector="main",
            semantic_keywords=("assistant", "response", "reply"),
            min_confidence=0.6,
        ),
        "new_chat_button": SelectorProfile(
            role="new_chat_button",
            primary=(
                'button[data-testid="new-chat"]',
                'a[href*="new"]',
            ),
            fallback=(
                '[class*="new-chat"]',
                "button",
                'a[href="/"]',
            ),
            expected_tag="button",
            expected_role="button",
            is_clickable=True,
            must_be_visible=True,
            semantic_keywords=("new", "chat"),
            min_confidence=0.7,
        ),
    }


def _glm_profiles() -> dict[str, SelectorProfile]:
    return {
        "message_input": SelectorProfile(
            role="message_input",
            primary=(
                'role=textbox[name="How can I help you today?"]',
                "#message-input",
                'textarea[placeholder*="help"]',
            ),
            fallback=(
                "textarea",
                '[role="textbox"]',
            ),
            expected_tag="textarea",
            expected_role="textbox",
            is_editable=True,
            must_be_visible=True,
            placeholder_hint="help",
            semantic_keywords=("help", "message", "ask"),
            min_confidence=0.7,
        ),
        "send_button": SelectorProfile(
            role="send_button",
            primary=(
                "#send-message-button",
                'role=button[name="Send Message"]',
            ),
            fallback=(
                "main button",
                'button:has(img[src*="send"])',
                '[aria-label*="send"]',
            ),
            expected_tag="button",
            expected_role="button",
            is_clickable=True,
            must_be_visible=True,
            semantic_keywords=("send", "submit"),
            min_confidence=0.7,
        ),
    }


def _yandex_profiles() -> dict[str, SelectorProfile]:
    return {
        "message_input": SelectorProfile(
            role="message_input",
            primary=(
                'textarea[placeholder*="Спросите"]',
                "textarea.AliceInput-Textarea",
            ),
            fallback=(
                "textarea",
                '[role="textbox"]',
            ),
            expected_tag="textarea",
            expected_role="textbox",
            is_editable=True,
            must_be_visible=True,
            placeholder_hint="Спросите",
            semantic_keywords=("спрос", "алис", "чём"),
            min_confidence=0.7,
        ),
        "send_button": SelectorProfile(
            role="send_button",
            primary=(),  # Yandex uses Enter key, not button
            fallback=(),
            expected_tag="",
            expected_role="",
            is_clickable=False,
            must_be_visible=False,
            min_confidence=0.0,  # Not applicable
        ),
    }


def _kimi_profiles() -> dict[str, SelectorProfile]:
    return {
        "message_input": SelectorProfile(
            role="message_input",
            primary=(
                'role=textbox',
                "#chat-box .chat-input-editor",
            ),
            fallback=(
                ".chat-input-editor",
                '[contenteditable="true"]',
            ),
            expected_tag="textarea",
            expected_role="textbox",
            is_editable=True,
            must_be_visible=True,
            container_selector="#chat-box",
            semantic_keywords=("chat", "message", "input"),
            min_confidence=0.7,
        ),
        "send_button": SelectorProfile(
            role="send_button",
            primary=(
                ".send-button-container:not(.disabled)",
                ".send-button-container",
            ),
            fallback=(
                ".send-icon",
                '[aria-label*="send"]',
            ),
            expected_tag="button",
            expected_role="button",
            is_clickable=True,
            must_be_visible=True,
            semantic_keywords=("send", "submit"),
            negative_keywords=("disabled",),
            min_confidence=0.7,
        ),
    }


def _deepseek_profiles() -> dict[str, SelectorProfile]:
    return {
        "message_input": SelectorProfile(
            role="message_input",
            primary=(
                'textarea[placeholder="Сообщение для DeepSeek"]',
                'textarea[placeholder*="DeepSeek"]',
            ),
            fallback=(
                "textarea",
                '[role="textbox"]',
            ),
            expected_tag="textarea",
            expected_role="textbox",
            is_editable=True,
            must_be_visible=True,
            placeholder_hint="DeepSeek",
            semantic_keywords=("deepseek", "сообщ", "message"),
            min_confidence=0.7,
        ),
        "send_button": SelectorProfile(
            role="send_button",
            primary=(
                '.ds-icon-button[role="button"][aria-disabled="false"]',
                '[role="button"][aria-disabled="false"]',
            ),
            fallback=(
                '[aria-disabled="false"]',
                ".ds-icon-button",
            ),
            expected_tag="button",
            expected_role="button",
            is_clickable=True,
            must_be_visible=True,
            semantic_keywords=("send", "submit"),
            negative_keywords=("disabled",),
            expected_attributes={"aria-disabled": "false"},
            min_confidence=0.7,
        ),
    }
